"""レーススケジュールと単勝オッズの取得(keiba-scraping を利用).

旧実装 scripts/odds/monitor_pre_race_odds.py の取得ロジックを、
単勝オッズ + 馬名だけに絞って移植したもの。
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime
from io import StringIO

import pandas as pd

from config import JRA_TRACKS, JST, ensure_scraping_on_path

ensure_scraping_on_path()

from scraping.config import ScrapingConfig  # noqa: E402
from scraping.entry_page import EntryPageScraper  # noqa: E402
from scraping.odds import scrape_odds_from_jra, scrape_odds_from_netkeiba  # noqa: E402
from scraping.race_schedule import RaceScheduleScraper  # noqa: E402

from jev_judge import Snapshot  # noqa: E402

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Race:
    race_id: str
    track: str
    number: int
    name: str
    start: datetime

    @property
    def label(self) -> str:
        return f"{self.track}{self.number}R"


def fetch_schedule(target_date: str, tracks: list[str] | None = None, races: list[int] | None = None) -> list[Race]:
    """対象日の JRA レース一覧を発走時刻つきで取得する."""
    year, month, day = (int(x) for x in target_date.split("-"))
    df = RaceScheduleScraper(year, month, day, ScrapingConfig()).get_race_schedule()
    if df.empty:
        return []

    allowed_tracks = {t.strip() for t in tracks or [] if t.strip()} or JRA_TRACKS
    allowed_races = set(races or [])
    result: list[Race] = []
    for _, row in df.iterrows():
        track = str(row["競馬場"]).strip()
        number = int(row["R"])
        if track not in allowed_tracks or track not in JRA_TRACKS:
            continue
        if allowed_races and number not in allowed_races:
            continue
        start = _parse_start(target_date, row["発走時刻"])
        if start is None:
            continue
        result.append(Race(str(row["レースID"]), track, number, str(row["レース名"]), start))
    return sorted(result, key=lambda r: (r.start, r.track))


def _parse_start(target_date: str, text: object) -> datetime | None:
    try:
        return datetime.strptime(f"{target_date} {str(text).strip()}", "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    except ValueError:
        return None


def _normalize_name(value: object) -> str:
    return re.sub(r"\s+", "", re.sub(r"\([^)]*\)", "", str(value)))


def fetch_horse_names(race_id: str) -> dict[int, str]:
    """出馬表から 馬番 -> 馬名 を取得する。失敗時は空 dict.

    EntryPageScraper.get_entry() は性齢の分解などで pandas のバージョン差の影響を受けやすいため、
    ページ取得だけ EntryPageScraper に任せ、必要な 2 列(馬番・馬名)は直接読む。
    列の位置は scraping.config.SHUTUBA_RAW_COLUMNS(枠, 馬番, 印, 馬名, ...)に従う。
    """
    try:
        html = EntryPageScraper(race_id, ScrapingConfig()).html_text
        table = pd.read_html(StringIO(html))[0]
        numbers = pd.to_numeric(table.iloc[:, 1], errors="coerce")
        result: dict[int, str] = {}
        for number, name in zip(numbers, table.iloc[:, 3]):
            if pd.notna(number) and pd.notna(name):
                result[int(number)] = _normalize_name(name)
        return result
    except Exception as exc:
        logger.warning("出馬表の取得に失敗 race_id=%s: %s", race_id, exc)
        return {}


def fetch_win_odds(race_id: str, names: dict[int, str] | None = None) -> tuple[Snapshot, str]:
    """単勝オッズを取得する。(馬番 -> (馬名, オッズ), ソース名)

    JRA 公式を優先し、失敗時は netkeiba にフォールバックする。
    どちらも失敗した場合は空 dict を返す(予想オッズは実オッズではないため使わない)。
    """
    names = names or {}
    config = ScrapingConfig()
    df = None
    source = ""

    try:
        df = asyncio.run(scrape_odds_from_jra(race_id, config))
        source = "JRA"
    except Exception as exc:
        logger.warning("JRA オッズ取得失敗 race_id=%s: %s", race_id, exc)

    if df is None or df.empty:
        try:
            df = scrape_odds_from_netkeiba(race_id, config)
            source = "netkeiba"
        except Exception as exc:
            logger.warning("netkeiba オッズ取得失敗 race_id=%s: %s", race_id, exc)

    if df is None or df.empty:
        return {}, ""

    snapshot: Snapshot = {}
    for _, row in df.iterrows():
        try:
            number = int(row["馬番"])
            odds = float(row["単勝オッズ"])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(odds) or odds <= 0:  # 取消・除外馬は NaN
            continue
        snapshot[number] = (names.get(number, ""), odds)
    return snapshot, source
