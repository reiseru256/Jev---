"""購入結果を ログ.txt に追記する.

1行1件。購入の成否・購入時オッズ・購入金額を必ず残す。
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from config import JST

# 結果ラベル
SUCCESS = "購入成功"
FAILED = "購入失敗"
UNKNOWN = "購入結果不明(要IPAT確認)"
SKIPPED = "見送り"
DRY_RUN = "DRY-RUN(未購入)"
PREPARED = "PREPARE-ONLY(未購入)"


class BetLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        result: str,
        track: str,
        race_no: int,
        horse_number: int | None = None,
        horse_name: str = "",
        odds: float | None = None,
        amount: int = 0,
        note: str = "",
    ) -> str:
        horse = "-" if horse_number is None else f"{horse_number}番 {horse_name}".strip()
        odds_text = "-" if odds is None else f"{odds:.1f}倍"
        line = (
            f"{datetime.now(JST):%Y-%m-%d %H:%M:%S} | 結果={result} | {track}{race_no}R | 単勝 {horse}"
            f" | 購入時オッズ={odds_text} | 購入金額={amount}円"
        )
        if note:
            line += f" | {note}"
        with self._lock, self.path.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")
        return line
