"""Jev 単勝自動購入 メインスクリプト.

流れ:
  1. レース30分前のオッズを取得
  2. レース10分前のオッズを取得
  3. レース4分前のオッズを取得
  4. 3時点のオッズから勝率が高い馬を Jev(TypeSafe AI の決定専用モデル)で判定し、購入対象を決める
  5. 購入対象の単勝を 100 円購入する
  6. 購入の成否・購入時オッズ・購入金額を ログ.txt に残す

実行モード(--mode):
  dry-run       (既定) オッズ取得と Jev 判定のみ。IPAT は開かず、購入もしない
  prepare-only  IPAT にログインして購入予定リストにセットするところまで。購入は確定しない
  live          実際に購入する
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from bet_logger import DRY_RUN, FAILED, PREPARED, SKIPPED, SUCCESS, UNKNOWN, BetLogger
from config import (
    DEFAULT_LOG_FILE,
    JST,
    ODDS_LOG_DIR,
    SNAPSHOT_TARGETS,
    STATE_DIR,
    IpatCredentials,
    JevConfig,
    jev_api_key,
    load_env,
)
from jev_client import JevClient
from jev_judge import Snapshot, judge
from odds_logger import OddsLogger

logger = logging.getLogger("jev_auto_bet")

PREWARM_MINUTES = 12.0  # 発走の何分前からログイン済みセッションを用意するか
FOUR_MIN_GRACE = next(grace for _, label, grace in SNAPSHOT_TARGETS if label == "4m")


@dataclass
class BotSettings:
    mode: str = "dry-run"
    amount: int = 100
    max_daily_bet: int = 3000
    min_minutes_to_bet: float = 1.5  # これより発走が近いと購入しない(IPAT の締切対策)
    poll_seconds: int = 20
    once: bool = False
    jev: JevConfig | None = None


class StateStore:
    """レース単位の取得済みオッズ・処理状況を JSON に保存する(再起動時の二重購入防止)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict = {"races": {}}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("state ファイルを読めなかったため初期化します: %s", path)
        self.data.setdefault("races", {})
        self.data.setdefault("spent", 0)

    def race(self, race_id: str) -> dict:
        return self.data["races"].setdefault(race_id, {"snapshots": {}, "names": {}, "status": ""})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")


def _to_snapshot(raw: dict) -> Snapshot:
    return {int(no): (name, float(odds)) for no, (name, odds) in raw.items()}


class Bot:
    def __init__(
        self,
        settings: BotSettings,
        races: list,
        state: StateStore,
        bet_logger: BetLogger,
        fetch_names: Callable[[str], dict[int, str]],
        fetch_odds: Callable[[str, dict[int, str]], tuple[Snapshot, str]],
        jev_client,
        odds_logger: OddsLogger,
        session=None,
        now_fn: Callable[[], datetime] = lambda: datetime.now(JST),
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.jev = settings.jev or JevConfig()
        self.races = races
        self.state = state
        self.bet_logger = bet_logger
        self.fetch_names = fetch_names
        self.fetch_odds = fetch_odds
        self.jev_client = jev_client
        self.odds_logger = odds_logger
        self.session = session
        self.now_fn = now_fn
        self.sleep_fn = sleep_fn

    # ------------------------------------------------------------------
    def run(self) -> None:
        try:
            while True:
                if not self._tick():
                    logger.info("対象レースの処理が完了しました")
                    break
                if self.settings.once:
                    logger.info("--once 指定のため 1 回のチェックで終了します")
                    break
                self.sleep_fn(self.settings.poll_seconds)
        finally:
            if self.session is not None:
                self.session.close()

    def _tick(self) -> bool:
        """1 回分の監視処理。未処理のレースが残っていれば True."""
        # 発走の早いレースから処理する(races は発走時刻順)。
        # 購入は判定直後に 1 件ずつ行い、他レースのオッズ取得を待たせない。
        for race in self.races:
            rs = self.state.race(race.race_id)
            if rs["status"]:
                continue
            now = self.now_fn()
            minutes_left = (race.start - now).total_seconds() / 60.0

            if minutes_left < -1:
                self._finish(race, rs, "skipped", SKIPPED, note="4分前オッズを取得できないまま発走")
                continue

            self._capture(race, rs, minutes_left)

            if "4m" in rs["snapshots"]:
                candidate = self._decide(race, rs, minutes_left)
                if candidate is not None:
                    self._purchase([candidate])
            elif minutes_left < 4 - FOUR_MIN_GRACE:
                self._finish(race, rs, "skipped", SKIPPED, note="4分前オッズの取得時間を過ぎました")

        if self._needs_prewarm(self.now_fn()):
            try:
                self.session.open()
            except Exception as exc:
                logger.warning("IPAT の事前ログインに失敗(購入時に再試行します): %s", exc)

        self.state.save()
        return any(not self.state.race(r.race_id)["status"] for r in self.races)

    # ------------------------------------------------------------------
    def _capture(self, race, rs: dict, minutes_left: float) -> None:
        for target, label, grace in SNAPSHOT_TARGETS:
            if label in rs["snapshots"]:
                continue
            if not (target - grace < minutes_left <= target):
                continue
            if not rs["names"]:
                rs["names"] = {str(no): name for no, name in self.fetch_names(race.race_id).items()}
            names = {int(no): name for no, name in rs["names"].items()}
            snapshot, source = self.fetch_odds(race.race_id, names)
            if not snapshot:
                logger.warning("%s %s: オッズ取得失敗(次回再試行)", race.label, label)
                continue
            rs["snapshots"][label] = {str(no): [name, odds] for no, (name, odds) in snapshot.items()}
            logger.info("CAPTURE %s %s 残り%.1f分 %d頭 source=%s", race.label, label, minutes_left, len(snapshot), source)
            self.odds_logger.write_snapshot(
                date=race.start.strftime("%Y-%m-%d"),
                track=race.track,
                race_no=race.number,
                label=label,
                snapshot=snapshot,
                source=source,
            )
            self.state.save()

    def _decide(self, race, rs: dict, minutes_left: float):
        snapshots = {label: _to_snapshot(raw) for label, raw in rs["snapshots"].items()}
        decision = judge(snapshots, self.jev_client, self.jev)
        logger.info("Jev判定 %s: %s", race.label, decision.reason)
        horse = decision.horse

        # Jev API の失敗は、購入できる時間が残っている間は次の tick で再試行する
        if decision.error and minutes_left >= self.settings.min_minutes_to_bet:
            return None

        if not decision.buy or horse is None:
            self._finish(
                race, rs, "skipped", SKIPPED,
                horse=horse, note=decision.reason,
            )
            return None
        if minutes_left < self.settings.min_minutes_to_bet:
            self._finish(
                race, rs, "skipped", SKIPPED,
                horse=horse, note=f"締切間近(残り{minutes_left:.1f}分)のため購入せず",
            )
            return None
        if self.state.data["spent"] + self.settings.amount > self.settings.max_daily_bet:
            self._finish(
                race, rs, "skipped", SKIPPED,
                horse=horse, note=f"1日の購入上限 {self.settings.max_daily_bet}円 に到達",
            )
            return None

        from ipat_client import BetOrder

        order = BetOrder(race.track, race.number, horse.number, self.settings.amount, horse.name)
        # 先に状態を保存しておく(購入中にクラッシュしても再起動時に二重購入しない)
        rs["status"] = "ordered"
        rs["horse"] = horse.number
        rs["odds"] = horse.odds
        self.state.save()
        return race, horse, order

    def _purchase(self, pending: list[tuple]) -> None:
        mode = self.settings.mode
        amount = self.settings.amount

        if mode == "dry-run":
            for race, horse, order in pending:
                rs = self.state.race(race.race_id)
                rs["status"] = "dry-run"
                self._log(
                    DRY_RUN, race, horse, amount,
                    note=f"Jev勝率={horse.win_prob:.1%} 確信度={horse.confidence:.2f} 期待値={horse.ev:.3f}",
                )
            return

        from ipat_client import IpatError, PurchaseUnconfirmed

        orders = [order for _, _, order in pending]
        by_order = {order: (race, horse) for race, horse, order in pending}
        confirm = mode == "live"
        try:
            result = self.session.place(orders, confirm=confirm)
        except PurchaseUnconfirmed as exc:
            for race, horse, order in pending:
                self.state.race(race.race_id)["status"] = "unknown"
                self._log(UNKNOWN, race, horse, amount, note=str(exc))
                self.state.data["spent"] += amount
            return
        except IpatError as exc:
            for race, horse, order in pending:
                self.state.race(race.race_id)["status"] = "failed"
                self._log(FAILED, race, horse, amount, note=str(exc))
            return
        finally:
            if mode == "prepare-only" and self.session is not None:
                self.session.close()  # セットしただけのカートを次回に持ち越さない

        for order in result.placed:
            race, horse = by_order[order]
            self.state.race(race.race_id)["status"] = "bought" if confirm else "prepared"
            self._log(SUCCESS if confirm else PREPARED, race, horse, amount)
            if confirm:
                self.state.data["spent"] += amount
        for order, message in result.errors.items():
            race, horse = by_order[order]
            self.state.race(race.race_id)["status"] = "failed"
            self._log(FAILED, race, horse, amount, note=message)

    # ------------------------------------------------------------------
    def _finish(self, race, rs: dict, status: str, result: str, horse=None, note: str = "") -> None:
        rs["status"] = status
        self._log(result, race, horse, 0, note=note)

    def _log(self, result: str, race, horse, amount: int, note: str = "") -> None:
        line = self.bet_logger.write(
            result=result,
            track=race.track,
            race_no=race.number,
            horse_number=horse.number if horse else None,
            horse_name=horse.name if horse else "",
            odds=horse.odds if horse else None,
            amount=amount,
            note=note,
        )
        logger.info(line)

    def _needs_prewarm(self, now: datetime) -> bool:
        if self.settings.mode == "dry-run" or self.session is None or self.session.is_open:
            return False
        return any(
            not self.state.race(r.race_id)["status"]
            and -1 < (r.start - now).total_seconds() / 60.0 <= PREWARM_MINUTES
            for r in self.races
        )


# ----------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    jev = JevConfig()
    p = argparse.ArgumentParser(description="Jev による単勝自動購入(30分前/10分前/4分前オッズで判定)")
    p.add_argument("--date", default=datetime.now(JST).strftime("%Y-%m-%d"), help="対象日 YYYY-MM-DD(既定: 今日)")
    p.add_argument("--tracks", nargs="*", default=[], help="対象競馬場。例: 阪神 中山(既定: 全JRA場)")
    p.add_argument("--races", nargs="*", type=int, default=[], help="対象レース番号。例: 11 12")
    p.add_argument("--mode", choices=["dry-run", "prepare-only", "live"], default="dry-run", help="live のみ実際に購入")
    p.add_argument("--amount", type=int, default=100, help="1レースの単勝購入金額(100円単位)")
    p.add_argument("--max-daily-bet", type=int, default=3000, help="1日の購入金額の上限")
    p.add_argument("--min-minutes-to-bet", type=float, default=1.5, help="発走まで何分を切ったら購入しないか")
    p.add_argument("--poll-seconds", type=int, default=20, help="監視間隔(秒)")
    p.add_argument("--once", action="store_true", help="1回だけチェックして終了")
    p.add_argument("--headless", action="store_true", help="ブラウザをヘッドレスで実行")
    p.add_argument("--chrome-driver-path", default=None, help="ChromeDriver のパス")
    p.add_argument("--log-file", type=Path, default=DEFAULT_LOG_FILE, help="購入ログの出力先(既定: ログ.txt)")
    g = p.add_argument_group("Jev 判定パラメータ")
    g.add_argument("--jev-model", default=jev.model, help="Jev のモデル名")
    g.add_argument("--min-confidence", type=float, default=jev.min_confidence, help="Jev の確信度の下限")
    g.add_argument("--min-win-prob", type=float, default=jev.min_win_prob, help="Jev が返した勝率の下限")
    g.add_argument("--min-odds", type=float, default=jev.min_odds, help="4分前オッズの下限")
    g.add_argument("--max-odds", type=float, default=jev.max_odds, help="4分前オッズの上限")
    g.add_argument("--min-ev", type=float, default=jev.min_ev, help="期待値(Jev勝率×オッズ)の下限")
    g.add_argument("--trend-pct", type=float, default=jev.trend_pct, help="売れた/離れた とみなすオッズ変化率(%%)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)

    if args.amount <= 0 or args.amount % 100 != 0:
        print("--amount は 100 円単位の正の値にしてください", file=sys.stderr)
        return 2

    env_path = load_env()
    logger.info("認証設定: %s", env_path)
    jev_config = JevConfig(
        model=args.jev_model,
        trend_pct=args.trend_pct,
        min_confidence=args.min_confidence,
        min_win_prob=args.min_win_prob,
        min_odds=args.min_odds,
        max_odds=args.max_odds,
        min_ev=args.min_ev,
    )
    try:
        jev_client = JevClient(
            jev_api_key(), model=jev_config.model, timeout=jev_config.timeout_seconds, max_attempts=jev_config.max_attempts
        )
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    session = None
    if args.mode != "dry-run":
        try:
            credentials = IpatCredentials.from_env()
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        from ipat_client import IpatSession

        session = IpatSession(credentials, headless=args.headless, chrome_driver_path=args.chrome_driver_path)

    from odds_fetcher import fetch_horse_names, fetch_schedule, fetch_win_odds

    races = fetch_schedule(args.date, args.tracks, args.races)
    if not races:
        print("対象のレースが見つかりませんでした。")
        return 0
    logger.info("対象 %d レース / mode=%s / 1レース%d円 / 上限%d円", len(races), args.mode, args.amount, args.max_daily_bet)
    if args.mode == "live":
        logger.warning("live モード: 実際に馬券を購入します")

    settings = BotSettings(
        mode=args.mode,
        amount=args.amount,
        max_daily_bet=args.max_daily_bet,
        min_minutes_to_bet=args.min_minutes_to_bet,
        poll_seconds=args.poll_seconds,
        once=args.once,
        jev=jev_config,
    )
    state = StateStore(STATE_DIR / f"state_{args.date.replace('-', '')}_{args.mode}.json")
    bot = Bot(
        settings=settings,
        races=races,
        state=state,
        bet_logger=BetLogger(args.log_file),
        fetch_names=fetch_horse_names,
        fetch_odds=fetch_win_odds,
        jev_client=jev_client,
        odds_logger=OddsLogger(ODDS_LOG_DIR),
        session=session,
    )
    try:
        bot.run()
    except KeyboardInterrupt:
        logger.info("中断しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
