"""Bot の時系列動作を、偽の取得関数・偽の IPAT セッション・偽の時計で検証する."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from auto_bet_main import Bot, BotSettings, StateStore
from bet_logger import BetLogger
from config import JST, JevConfig
from fakes import FakeJev
from odds_logger import OddsLogger
from jev_client import JevError
from ipat_client import (
    BetOrder,
    IpatError,
    PlaceResult,
    PurchaseUnconfirmed,
    bet_submission_detected,
    extract_bet_summary,
    purchase_completion_detected,
    race_label_is_closed,
    race_text_matches,
)
from odds_fetcher_stub import Race  # noqa: E402  (tests/odds_fetcher_stub.py)


START = datetime(2026, 9, 19, 10, 0, tzinfo=JST)

TAIL = (15.0, 20.0, 30.0, 40.0, 60.0, 80.0)  # 6〜11番人気。1/オッズの合計が現実的(約1.2)になるようにする


def field(*head):
    return {i + 1: (f"馬{i + 1}", o) for i, o in enumerate((*head, *TAIL))}


# 1番人気が 3.6倍 → 2.5倍 と売れていく
ODDS_BY_LABEL = {
    "30m": field(3.6, 4.0, 6.0, 9.0, 12.0),
    "10m": field(3.0, 4.2, 6.4, 9.0, 12.5),
    "4m": field(2.5, 4.5, 6.8, 9.5, 13.0),
}


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, minutes):
        self.now += timedelta(minutes=minutes)


class FakeSession:
    def __init__(self, error=None, closed=False):
        self.error = error
        self.calls = []
        self.opened = False
        self.closed = 0

    @property
    def is_open(self):
        return self.opened

    def open(self):
        self.opened = True

    def close(self):
        self.opened = False
        self.closed += 1

    def place(self, orders, confirm=True):
        self.calls.append((list(orders), confirm))
        if self.error:
            raise self.error
        return PlaceResult(placed=list(orders))


def make_bot(tmp_path, mode, session=None, clock=None, minutes_start=31.0, jev=None, jev_client=None):
    clock = clock or Clock(START - timedelta(minutes=minutes_start))
    race = Race("202609010111", "阪神", 11, "テストS", START)
    odds_calls = []

    def fetch_odds(race_id, names):
        # 現在の残り分に応じたスナップショットを返す
        left = (START - clock.now).total_seconds() / 60
        label = "30m" if left > 20 else "10m" if left > 6 else "4m"
        odds_calls.append(label)
        return dict(ODDS_BY_LABEL[label]), "TEST"

    bot = Bot(
        settings=BotSettings(mode=mode, poll_seconds=0, jev=jev or JevConfig()),
        races=[race],
        state=StateStore(tmp_path / "state.json"),
        bet_logger=BetLogger(tmp_path / "ログ.txt"),
        fetch_names=lambda race_id: {},
        fetch_odds=fetch_odds,
        jev_client=jev_client or FakeJev(pick=1, prob=0.42),
        odds_logger=OddsLogger(tmp_path),
        session=session,
        now_fn=clock,
        sleep_fn=lambda s: clock.advance(0.5),  # 30秒ずつ進める
    )
    return bot, clock, odds_calls, tmp_path / "ログ.txt"


def test_dry_run_full_flow(tmp_path):
    bot, clock, odds_calls, log = make_bot(tmp_path, "dry-run")
    bot.run()
    assert odds_calls == ["30m", "10m", "4m"]  # 各時点 1 回ずつ
    text = log.read_text(encoding="utf-8")
    assert "DRY-RUN(未購入)" in text
    assert "阪神11R" in text and "1番 馬1" in text
    assert "購入時オッズ=2.5倍" in text and "購入金額=100円" in text


def test_live_success_logged_and_single_order(tmp_path):
    session = FakeSession()
    bot, _, _, log = make_bot(tmp_path, "live", session=session)
    bot.run()
    assert len(session.calls) == 1
    orders, confirm = session.calls[0]
    assert confirm is True
    assert orders == [BetOrder("阪神", 11, 1, 100, "馬1")]
    assert "結果=購入成功" in log.read_text(encoding="utf-8")
    assert bot.state.data["spent"] == 100


def test_prepare_only_does_not_confirm(tmp_path):
    session = FakeSession()
    bot, _, _, log = make_bot(tmp_path, "prepare-only", session=session)
    bot.run()
    assert session.calls[0][1] is False
    assert "PREPARE-ONLY(未購入)" in log.read_text(encoding="utf-8")
    assert bot.state.data["spent"] == 0


def test_purchase_failure_is_logged(tmp_path):
    session = FakeSession(error=IpatError("ログインに失敗しました"))
    bot, _, _, log = make_bot(tmp_path, "live", session=session)
    bot.run()
    text = log.read_text(encoding="utf-8")
    assert "結果=購入失敗" in text and "ログインに失敗" in text
    assert bot.state.data["spent"] == 0


def test_unconfirmed_purchase_is_never_retried_and_counted(tmp_path):
    session = FakeSession(error=PurchaseUnconfirmed("購入受付の確認ができませんでした"))
    bot, _, _, log = make_bot(tmp_path, "live", session=session)
    bot.run()
    assert len(session.calls) == 1
    assert "購入結果不明" in log.read_text(encoding="utf-8")
    assert bot.state.data["spent"] == 100  # 購入済みの可能性があるので上限に算入


def test_restart_does_not_double_buy(tmp_path):
    session = FakeSession()
    bot, clock, _, _ = make_bot(tmp_path, "live", session=session)
    bot.run()
    # 同じ state ファイルで再起動しても購入済みレースは触らない
    session2 = FakeSession()
    bot2, _, _, _ = make_bot(tmp_path, "live", session=session2)
    bot2.run()
    assert session2.calls == []


def test_skipped_when_started_too_late(tmp_path):
    # 4分前を過ぎてから起動 → 30m/10m/4m のどれも取れず見送り
    session = FakeSession()
    bot, _, odds_calls, log = make_bot(tmp_path, "live", session=session, minutes_start=1.0)
    bot.run()
    assert session.calls == []
    assert "見送り" in log.read_text(encoding="utf-8")


def test_daily_limit_blocks_purchase(tmp_path):
    session = FakeSession()
    bot, _, _, log = make_bot(tmp_path, "live", session=session)
    bot.settings.max_daily_bet = 0
    bot.run()
    assert session.calls == []
    assert "上限" in log.read_text(encoding="utf-8")


def test_prewarm_opens_session_before_4m(tmp_path):
    session = FakeSession()
    bot, clock, _, _ = make_bot(tmp_path, "live", session=session, minutes_start=11.0)
    bot._tick()
    assert session.opened


# ---- IPAT ページ本文の判定 ----
def test_bet_order_validation():
    with pytest.raises(ValueError):
        BetOrder("阪神", 11, 1, 150)
    with pytest.raises(ValueError):
        BetOrder("阪神", 11, 0, 100)


def test_page_text_helpers():
    assert race_text_matches("11R 15:40", 11) and not race_text_matches("111R", 11)
    assert race_label_is_closed("11R 締切")
    assert purchase_completion_detected("受付番号 12345")
    assert not purchase_completion_detected("投票内容がありません")
    assert extract_bet_summary("1件 組合せ確認 合計金額: 100円") == (1, 100)
    assert bet_submission_detected("1件 組合せ確認 合計金額: 100円", 100, 0, 0)
    assert not bet_submission_detected("投票内容がありません", 100, 0, 0)


# ---- Jev 連携 ----
def test_jev_api_error_is_retried_on_next_tick_until_deadline(tmp_path):
    class FlakyJev(FakeJev):
        def ask_choice(self, *args):
            if len(self.calls) < 2:  # 最初の 2 回は失敗
                self.calls.append(args)
                raise JevError("HTTP 529")
            return super().ask_choice(*args)

    session = FakeSession()
    jev = FlakyJev(pick=1, prob=0.42)
    bot, _, _, log = make_bot(tmp_path, "live", session=session, jev_client=jev)
    bot.run()
    assert len(jev.calls) == 3  # 失敗 2 回 → 3 回目で成功して購入
    assert len(session.calls) == 1
    assert "結果=購入成功" in log.read_text(encoding="utf-8")


def test_jev_api_down_until_deadline_skips_without_buying(tmp_path):
    session = FakeSession()
    jev = FakeJev(error=JevError("HTTP 529"))
    bot, _, _, log = make_bot(tmp_path, "live", session=session, jev_client=jev)
    bot.run()
    assert session.calls == []
    text = log.read_text(encoding="utf-8")
    assert "結果=見送り" in text and "Jev API エラー" in text


def test_jev_says_no_means_no_purchase(tmp_path):
    session = FakeSession()
    bot, _, _, log = make_bot(tmp_path, "live", session=session, jev_client=FakeJev(pick=1, prob=0.10))
    bot.run()
    assert session.calls == []
    assert "Jev勝率" in log.read_text(encoding="utf-8")
