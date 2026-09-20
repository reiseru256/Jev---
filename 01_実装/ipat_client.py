"""即パット(IPAT)で単勝を購入するクライアント.

旧実装 keiba-auto-bet/keiba_auto_bet/auto_bet.py の画面操作(セレクタ)を移植し、
次の点を変えている。

* ブラウザ/ログインを保持する(IpatSession)。4分前判定から締切までが短いため、
  購入の都合でその都度ログインし直さない。
* 購入ボタンを押した後に完了を確認できなかった場合は PurchaseUnconfirmed を送出する。
  この場合は購入済みの可能性があるので、呼び出し側は再購入してはならない。
* 単勝専用。
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field

from selenium import webdriver
from selenium.common.exceptions import (
    NoAlertPresentException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import Select, WebDriverWait

from config import IPAT_URL, IpatCredentials

TIMEOUT = 10
MAX_RETRIES = 3
RETRY_INTERVAL = 1.0
WIN_LABEL = "単勝"  # 馬券種プルダウンの表示名


class IpatError(Exception):
    """IPAT 操作の失敗(購入は成立していない)."""


class RaceClosedError(IpatError):
    """対象レースが既に締切."""


class PurchaseUnconfirmed(IpatError):
    """購入ボタン押下後に完了を確認できなかった。購入済みの可能性がある."""


@dataclass(frozen=True)
class BetOrder:
    venue: str
    race_number: int
    horse_number: int
    amount: int
    horse_name: str = ""

    def __post_init__(self) -> None:
        if self.amount <= 0 or self.amount % 100 != 0:
            raise ValueError(f"購入金額は100円単位の正の値にしてください: {self.amount}")
        if self.horse_number <= 0 or self.race_number <= 0:
            raise ValueError("馬番・レース番号が不正です")


@dataclass
class PlaceResult:
    placed: list[BetOrder] = field(default_factory=list)
    errors: dict[BetOrder, str] = field(default_factory=dict)


# --------------------------------------------------------------------------
# ページ本文の判定(純粋関数)
# --------------------------------------------------------------------------
def _normalize(text: object) -> str:
    return " ".join(str(text or "").split())


def race_text_matches(text: str, race_number: int) -> bool:
    return re.search(rf"(?<!\d)0*{race_number}R(?!\d)", _normalize(text)) is not None


def race_label_is_closed(text: str) -> bool:
    normalized = _normalize(text)
    return "締切" in normalized or "発売終了" in normalized


def purchase_completion_detected(text: str) -> bool:
    normalized = _normalize(text)
    markers = ("投票を受け付けました", "購入を受け付けました", "投票が完了", "購入完了")
    if any(marker in normalized for marker in markers):
        return True
    return re.search(r"受付番号\s*[:：]?\s*\d{4,}", normalized) is not None


def extract_bet_summary(text: str) -> tuple[int, int]:
    """購入予定リストの (件数, 合計金額) を抽出する."""
    normalized = _normalize(text)
    count_match = re.search(r"(\d+)件\s*組合せ確認", normalized)
    count = int(count_match.group(1)) if count_match else 0
    amounts = [
        int(m.group(1).replace(",", ""))
        for m in re.finditer(r"合計金額(?:入力)?\s*[:：]?\s*([0-9,]+)円", normalized)
        if int(m.group(1).replace(",", "")) > 0
    ]
    return count, max(amounts, default=0)


def bet_submission_detected(text: str, expected_amount: int, previous_count: int, previous_total: int) -> bool:
    """セット操作が購入予定リストに反映されたか."""
    normalized = _normalize(text)
    if "投票内容がありません" in normalized:
        return False
    count, total = extract_bet_summary(normalized)
    if count > previous_count:
        return True
    if total > 0 and total >= previous_total + max(100, expected_amount):
        return True
    return count > 0 and total >= max(100, expected_amount)


def _option_label(element) -> str:
    for candidate in (
        getattr(element, "text", ""),
        element.get_attribute("label"),
        element.get_attribute("textContent"),
        element.get_attribute("innerText"),
    ):
        normalized = _normalize(candidate)
        if normalized:
            return normalized
    return ""


def _discover_chrome_binary() -> str | None:
    for env in ("GOOGLE_CHROME_BIN", "CHROME_BIN", "CHROMIUM_PATH"):
        path = os.environ.get(env)
        if path and os.path.exists(path):
            return path
    for command in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        resolved = shutil.which(command)
        if resolved:
            return resolved
    for pattern in (
        r"C:/Program Files/Google/Chrome/Application/chrome.exe",
        r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    ):
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return None


# --------------------------------------------------------------------------
# セッション
# --------------------------------------------------------------------------
class IpatSession:
    """ログイン済みブラウザを保持して単勝を購入する."""

    def __init__(
        self,
        credentials: IpatCredentials,
        headless: bool = False,
        chrome_driver_path: str | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._credentials = credentials
        self._headless = headless
        self._chrome_driver_path = chrome_driver_path
        self._logger = logger or logging.getLogger(__name__)
        self._driver: webdriver.Chrome | None = None

    @property
    def is_open(self) -> bool:
        return self._driver is not None

    # ---- ライフサイクル ----
    def open(self) -> None:
        """Chrome を起動し、ログインしてトップ画面まで進む."""
        if self._driver is not None:
            return
        self._start_chrome()
        try:
            self._login()
            self._dismiss_announce_page()
        except Exception:
            self.close()
            raise
        self._logger.info("IPAT にログインしました")

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

    def place(self, orders: list[BetOrder], confirm: bool = True) -> PlaceResult:
        """注文をセットし、confirm=True なら購入まで行う.

        セット・ログイン段階の失敗は 1 回だけセッションを作り直して再試行する。
        購入ボタン押下後の失敗(PurchaseUnconfirmed)は再試行しない。
        """
        if not orders:
            return PlaceResult()
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                self.open()
                return self._place_once(orders, confirm)
            except PurchaseUnconfirmed:
                self.close()
                raise
            except RaceClosedError:
                # 締切は再試行しても変わらない。セッションは残し、次の購入のためトップ画面へ戻す
                try:
                    self._navigate_to_top()
                except IpatError:
                    self.close()
                raise
            except IpatError as exc:
                last_error = exc
                self._logger.warning("購入処理に失敗(%d/2): %s", attempt + 1, exc)
                self.close()
        assert last_error is not None
        raise last_error

    # ---- 起動・ログイン ----
    def _start_chrome(self) -> None:
        options = Options()
        if self._headless:
            options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        binary = _discover_chrome_binary()
        if binary:
            options.binary_location = binary

        service = Service(self._chrome_driver_path) if self._chrome_driver_path else Service()
        try:
            self._driver = webdriver.Chrome(service=service, options=options)
            self._driver.get(IPAT_URL)
            WebDriverWait(self._driver, TIMEOUT).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception as exc:
            self.close()
            raise IpatError(f"Chrome の起動に失敗しました: {exc}") from exc

    def _login(self) -> None:
        driver = self._require_driver()
        try:
            WebDriverWait(driver, TIMEOUT).until(
                ec.presence_of_element_located((By.NAME, "inetid"))
            ).send_keys(self._credentials.inet_id)
            WebDriverWait(driver, TIMEOUT).until(
                ec.element_to_be_clickable((By.XPATH, "//a[@title='ログイン' and @tabindex='4']"))
            ).click()

            WebDriverWait(driver, TIMEOUT).until(
                ec.presence_of_element_located((By.NAME, "i"))
            ).send_keys(self._credentials.user_number)
            driver.find_element(By.NAME, "p").send_keys(self._credentials.password)
            driver.find_element(By.NAME, "r").send_keys(self._credentials.p_ars)

            menu_link = WebDriverWait(driver, TIMEOUT).until(
                ec.element_to_be_clickable((By.XPATH, "//a[@title='ネット投票メニューへ' and @tabindex='5']"))
            )
            menu_link.click()
            WebDriverWait(driver, TIMEOUT).until(ec.staleness_of(menu_link))
        except Exception as exc:
            raise IpatError(f"ログインに失敗しました: {exc}") from exc

    def _dismiss_announce_page(self) -> None:
        """ログイン直後のお知らせページがあれば閉じる."""
        driver = self._require_driver()
        if not driver.find_elements(By.XPATH, "//h1[contains(text(), 'お知らせ')]"):
            return
        try:
            ok_button = WebDriverWait(driver, TIMEOUT).until(
                ec.element_to_be_clickable((By.CSS_SELECTOR, "button.btn-ok"))
            )
            ok_button.click()
            WebDriverWait(driver, TIMEOUT).until(ec.staleness_of(ok_button))
        except Exception as exc:
            raise IpatError(f"お知らせページの処理に失敗しました: {exc}") from exc

    # ---- 購入 ----
    def _place_once(self, orders: list[BetOrder], confirm: bool) -> PlaceResult:
        self._navigate_to_bet_page()
        result = PlaceResult()
        closed_only = True
        # 締切の遅い(後半の)レースから先に処理する
        for order in sorted(orders, key=lambda o: (o.venue, -o.race_number)):
            try:
                self._select_race(order.venue, order.race_number)
                self._set_win_ticket(order.horse_number, order.amount)
                result.placed.append(order)
                self._logger.info(
                    "%s%dR 単勝 %d番 %d円 をセットしました", order.venue, order.race_number, order.horse_number, order.amount
                )
            except IpatError as exc:
                closed_only = closed_only and isinstance(exc, RaceClosedError)
                result.errors[order] = str(exc)
                self._logger.warning("%s%dR はスキップ: %s", order.venue, order.race_number, exc)

        if not result.placed:
            reasons = "; ".join(result.errors.values())
            if closed_only:
                raise RaceClosedError(f"購入可能な注文がありません: {reasons}")
            raise IpatError(f"購入可能な注文がありません: {reasons}")

        if confirm:
            self._confirm_purchase(sum(o.amount for o in result.placed))
            try:
                self._navigate_to_top()
            except IpatError as exc:
                self._logger.warning("購入後のトップ画面復帰に失敗(購入自体は完了): %s", exc)
        return result

    def _navigate_to_bet_page(self) -> None:
        driver = self._require_driver()
        try:
            WebDriverWait(driver, TIMEOUT).until(
                ec.element_to_be_clickable((By.XPATH, "//button[@title='出馬表から馬を選択する方式です。']"))
            ).click()
            WebDriverWait(driver, TIMEOUT).until(
                ec.presence_of_element_located((By.ID, "select-course-race-course"))
            )
            self._wait_for_element_stable(By.ID, "select-course-race-course")
        except Exception as exc:
            raise IpatError(f"購入画面への移動に失敗しました: {exc}") from exc

    def _select_race(self, venue: str, race_number: int) -> None:
        """場名・レース番号をボタンで選ぶ(IPATのUIはプルダウンではなくボタン形式).

        場が複数開催(例: 阪神(日)/阪神(月))のときは、最初に一致したボタンを使う
        (旧実装のプルダウンでの「最初の一致を選ぶ」挙動を踏襲)。
        """
        driver = self._require_driver()
        try:
            course_buttons = WebDriverWait(driver, TIMEOUT).until(
                lambda d: d.find_elements(By.CSS_SELECTOR, "div.place-btn-area button") or False
            )
            course_button = next((b for b in course_buttons if venue in b.text), None)
            if course_button is None:
                raise IpatError(f"競馬場が見つかりませんでした: {venue}")
            course_button.click()

            self._wait_for_no_loading_overlay()
            race_buttons = WebDriverWait(driver, TIMEOUT).until(
                lambda d: d.find_elements(By.CSS_SELECTOR, "div.races button") or False
            )
            labels = [_normalize(b.text) for b in race_buttons]
            for button, label in zip(race_buttons, labels):
                if not race_text_matches(label, race_number):
                    continue
                if race_label_is_closed(label) or button.get_attribute("disabled") is not None:
                    raise RaceClosedError(f"{venue}{race_number}R は既に締切です")
                button.click()
                return
            raise IpatError(f"レースが見つかりませんでした: {race_number}R / 選択肢: {', '.join(labels)}")
        except IpatError:
            raise
        except Exception as exc:
            raise IpatError(f"レース選択に失敗しました: {exc}") from exc

    def _set_win_ticket(self, horse_number: int, amount: int) -> None:
        """単勝・馬番・金額を入力して購入予定リストにセットする."""
        driver = self._require_driver()
        last_body = ""
        for attempt in range(MAX_RETRIES):
            try:
                self._wait_for_no_loading_overlay()
                before_text = driver.find_element(By.TAG_NAME, "body").text
                before_count, before_total = extract_bet_summary(before_text)

                bet_type = Select(
                    WebDriverWait(driver, TIMEOUT).until(ec.element_to_be_clickable((By.ID, "bet-basic-type")))
                )
                bet_type.select_by_visible_text(WIN_LABEL)

                input_id = f"no{horse_number}"
                label_element = WebDriverWait(driver, TIMEOUT).until(
                    ec.presence_of_element_located((By.XPATH, f"//label[@for='{input_id}']"))
                )
                selected = driver.execute_script(
                    "const label = arguments[0]; const input = document.getElementById(arguments[1]);"
                    "if (!input) { return false; }"
                    "input.scrollIntoView({block: 'center'});"
                    "if (!input.checked) { input.click(); }"
                    "if (!input.checked && label) { label.click(); }"
                    "input.dispatchEvent(new Event('input', {bubbles: true}));"
                    "input.dispatchEvent(new Event('change', {bubbles: true}));"
                    "return !!input.checked;",
                    label_element,
                    input_id,
                )
                if not selected:
                    raise IpatError(f"馬番の選択が反映されませんでした: {horse_number}番")

                amount_input = WebDriverWait(driver, TIMEOUT).until(
                    ec.element_to_be_clickable((By.XPATH, "//input[@maxlength='4' and @ng-model='vm.nUnit']"))
                )
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});"
                    "arguments[0].value = '';"
                    "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));"
                    "arguments[0].value = arguments[1];"
                    "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));"
                    "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));"
                    "arguments[0].dispatchEvent(new Event('blur', {bubbles: true}));",
                    amount_input,
                    str(amount // 100),
                )

                set_button = WebDriverWait(driver, TIMEOUT).until(
                    ec.element_to_be_clickable(
                        (By.CSS_SELECTOR, "button.btn.btn-lg.btn-set.btn-primary[ng-click='vm.onSet()']")
                    )
                )
                self._wait_for_no_loading_overlay()
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", set_button)
                driver.execute_script("arguments[0].click();", set_button)
                self._wait_for_no_loading_overlay()

                WebDriverWait(driver, TIMEOUT).until(
                    lambda d: bet_submission_detected(
                        d.find_element(By.TAG_NAME, "body").text, amount, before_count, before_total
                    )
                )
                WebDriverWait(driver, TIMEOUT).until(ec.element_to_be_clickable((By.ID, "bet-basic-type")))
                return
            except Exception as exc:
                try:
                    last_body = driver.find_element(By.TAG_NAME, "body").text[:500]
                except Exception:
                    last_body = ""
                if attempt == MAX_RETRIES - 1:
                    detail = f" / body={last_body}" if last_body else ""
                    raise IpatError(f"単勝 {horse_number}番 {amount}円 のセットに失敗: {exc}{detail}") from exc
                self._logger.warning("セット反映を再試行 (%d/%d): %s", attempt + 1, MAX_RETRIES, exc)
                time.sleep(RETRY_INTERVAL)

    def _confirm_purchase(self, total_amount: int) -> None:
        """購入予定リストの合計金額を入力して購入を確定する."""
        driver = self._require_driver()

        # ---- ボタン押下前: 失敗しても購入は成立していない ----
        try:
            vote_buttons = driver.find_elements(By.XPATH, "//button[contains(@class, 'btn-vote-list')]")
            if vote_buttons:
                self._wait_for_no_loading_overlay()
                driver.execute_script("arguments[0].click();", vote_buttons[0])
                self._wait_for_no_loading_overlay()

            try:
                WebDriverWait(driver, 8).until(
                    lambda d: "投票内容がありません" not in d.find_element(By.TAG_NAME, "body").text
                )
            except TimeoutException:
                raise IpatError("購入予定リストが空です。セットが反映されませんでした")

            total_input = self._find_total_amount_input()
            try:
                total_input.clear()
                total_input.send_keys(str(total_amount))
            except Exception:
                driver.execute_script(
                    "arguments[0].value = '';"
                    "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));"
                    "arguments[0].value = arguments[1];"
                    "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));"
                    "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));"
                    "arguments[0].dispatchEvent(new Event('blur', {bubbles: true}));",
                    total_input,
                    str(total_amount),
                )

            purchase_button = WebDriverWait(driver, TIMEOUT).until(
                ec.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//button[contains(@class, 'btn-primary') and "
                        "(contains(normalize-space(.), '購入する') or contains(normalize-space(.), '投票する'))]",
                    )
                )
            )
        except IpatError:
            raise
        except Exception as exc:
            raise IpatError(f"購入確定の準備に失敗しました: {exc}") from exc

        # ---- ボタン押下後: 以降の失敗は購入済みの可能性がある ----
        try:
            driver.execute_script("arguments[0].click();", purchase_button)
            if self._wait_for_completion():
                self._logger.info("購入受付を確認しました")
                return
        except Exception as exc:
            raise PurchaseUnconfirmed(f"購入確定中にエラー(購入済みの可能性あり): {exc}") from exc
        raise PurchaseUnconfirmed("購入受付の確認ができませんでした(購入済みの可能性あり)")

    def _wait_for_completion(self) -> bool:
        driver = self._require_driver()
        confirm_xpaths = [
            f"//div[contains(@class, 'dialog')]//button[contains(normalize-space(.), '{text}')]"
            for text in ("OK", "はい", "投票する", "確認")
        ]
        deadline = time.time() + TIMEOUT
        while time.time() < deadline:
            try:
                alert = driver.switch_to.alert
                alert_text = alert.text or ""
                alert.accept()
                time.sleep(1)
                if purchase_completion_detected(alert_text):
                    return True
            except NoAlertPresentException:
                pass
            except Exception:
                pass

            if purchase_completion_detected(driver.page_source):
                return True

            clicked = False
            for xpath in confirm_xpaths:
                for button in driver.find_elements(By.XPATH, xpath):
                    try:
                        label = _option_label(button)
                        if label and "閉じる" not in label and button.is_displayed() and button.is_enabled():
                            driver.execute_script("arguments[0].click();", button)
                            self._logger.info("購入確認ボタンを押しました: %s", label)
                            clicked = True
                            time.sleep(1)
                            break
                    except Exception:
                        continue
                if clicked:
                    break
            if not clicked:
                time.sleep(0.5)
        return False

    def _find_total_amount_input(self):
        driver = self._require_driver()
        xpaths = [
            "//input[@ng-model='vm.cAmountTotal']",
            "//input[contains(@ng-model, 'AmountTotal')]",
            "//input[@name='cAmountTotal']",
            "//input[(contains(@placeholder, '金額') or contains(@aria-label, '金額') or contains(@title, '金額')) and not(@disabled)]",
            "//input[((@type='tel' or @type='number' or @inputmode='numeric' or @maxlength='4' or @maxlength='5') and not(@disabled))]",
        ]
        for xpath in xpaths:
            try:
                return WebDriverWait(driver, TIMEOUT // 2).until(ec.element_to_be_clickable((By.XPATH, xpath)))
            except Exception:
                continue
        raise IpatError("合計金額入力フィールドが見つかりません")

    def _navigate_to_top(self) -> None:
        driver = self._require_driver()
        try:
            WebDriverWait(driver, TIMEOUT).until(
                ec.element_to_be_clickable((By.XPATH, "//a[@ui-sref='home' and @ng-click='vm.clickLogo()']"))
            ).click()
            WebDriverWait(driver, TIMEOUT).until(
                ec.element_to_be_clickable((By.XPATH, "//button[@title='出馬表から馬を選択する方式です。']"))
            )
        except Exception as exc:
            raise IpatError(f"トップ画面への遷移に失敗しました: {exc}") from exc

    # ---- 待機ヘルパー ----
    def _wait_for_no_loading_overlay(self) -> None:
        driver = self._require_driver()
        try:
            WebDriverWait(driver, TIMEOUT).until(
                lambda d: all(
                    not elem.is_displayed()
                    for elem in d.find_elements(By.CSS_SELECTOR, ".ipat-loading, [ng-if='isLoading']")
                )
            )
        except Exception:
            pass

    def _wait_for_element_stable(self, by: str, value: str) -> None:
        """AngularJS の再描画が落ち着いて要素が触れるようになるまで待つ."""
        driver = self._require_driver()
        end = time.time() + TIMEOUT
        while time.time() < end:
            try:
                element = driver.find_element(by, value)
                element.is_displayed()
                time.sleep(0.5)
                element.is_displayed()
                return
            except (NoSuchElementException, StaleElementReferenceException):
                time.sleep(0.5)
        raise TimeoutException(f"要素 {value} の安定化待機がタイムアウトしました")

    def _require_driver(self) -> webdriver.Chrome:
        if self._driver is None:
            raise IpatError("ブラウザが起動していません")
        return self._driver
