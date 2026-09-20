"""取得した単勝オッズを 03_取得ログ フォルダに CSV で残す.

1 行 = 1 レース・1 取得タイミング(30分前/10分前/4分前)・1 頭。
後から Jev の勝率が実際の勝率と合っているかを検証できるように、
判定に使う前の生オッズをそのまま残す。
"""

from __future__ import annotations

import csv
import threading
from datetime import datetime
from pathlib import Path

from config import JST
from jev_judge import Snapshot

FIELDS = ["取得日時", "日付", "競馬場", "R", "タイミング", "馬番", "馬名", "単勝オッズ", "取得元"]


class OddsLogger:
    def __init__(self, dir_path: Path) -> None:
        self.dir_path = dir_path
        self._lock = threading.Lock()

    def write_snapshot(
        self,
        date: str,
        track: str,
        race_no: int,
        label: str,
        snapshot: Snapshot,
        source: str,
    ) -> None:
        """1 回分の取得(ある race の ある label)を、馬ごとに 1 行ずつ追記する."""
        if not snapshot:
            return
        path = self.dir_path / f"取得オッズ_{date.replace('-', '')}.csv"
        now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        rows = [
            {
                "取得日時": now,
                "日付": date,
                "競馬場": track,
                "R": race_no,
                "タイミング": label,
                "馬番": number,
                "馬名": name,
                "単勝オッズ": odds,
                "取得元": source,
            }
            for number, (name, odds) in sorted(snapshot.items())
        ]
        with self._lock:
            self.dir_path.mkdir(parents=True, exist_ok=True)
            is_new = not path.exists()
            with path.open("a", encoding="utf-8-sig", newline="") as fp:
                writer = csv.DictWriter(fp, fieldnames=FIELDS)
                if is_new:
                    writer.writeheader()
                writer.writerows(rows)
