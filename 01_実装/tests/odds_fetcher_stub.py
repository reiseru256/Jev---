"""テスト用: odds_fetcher.Race と同じ形の軽量クラス(keiba-scraping / Selenium を import しないため)."""

from dataclasses import dataclass
from datetime import datetime


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
