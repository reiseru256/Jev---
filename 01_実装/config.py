"""共通設定・定数."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

APP_DIR = Path(__file__).resolve().parent
KEIBA_ROOT = APP_DIR.parents[1]
SCRAPING_DIR = KEIBA_ROOT / "keiba-scraping"
LEGACY_DIR = KEIBA_ROOT / "旧実装"

DEFAULT_LOG_FILE = APP_DIR / "ログ.txt"
STATE_DIR = APP_DIR / "state"
ODDS_LOG_DIR = APP_DIR.parent / "03_取得ログ"

# 認証情報(.env)の探索順。旧実装の .env は後方互換のためのフォールバック。
ENV_CANDIDATES = [
    APP_DIR / ".env",
    LEGACY_DIR / "keiba-auto-bet" / ".env",
]

IPAT_URL = "https://www.ipat.jra.go.jp/"

# オッズを取得するタイミング (発走の何分前か, ラベル, 許容する遅れ[分])。
# 「target 分前を過ぎてから (target - 許容) 分前まで」の間に最初に見つけたときに取得する。
# 4分前は購入に使う時間を残すため許容を短くしている。
SNAPSHOT_TARGETS: tuple[tuple[int, str, float], ...] = (
    (30, "30m", 3.0),
    (10, "10m", 3.0),
    (4, "4m", 1.5),
)

# IPAT で購入できる JRA 10 場
JRA_TRACKS = {"札幌", "函館", "福島", "新潟", "東京", "中山", "中京", "京都", "阪神", "小倉"}


def ensure_scraping_on_path() -> None:
    """keiba-scraping ライブラリを import できるようにする."""
    path = str(SCRAPING_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)


def load_env() -> Path | None:
    """最初に見つかった .env を読み込み、そのパスを返す."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return None
    for candidate in ENV_CANDIDATES:
        if candidate.exists():
            load_dotenv(candidate)
            return candidate
    return None


@dataclass
class IpatCredentials:
    inet_id: str
    user_number: str
    password: str
    p_ars: str

    @classmethod
    def from_env(cls) -> "IpatCredentials":
        values = {
            "IPAT_INET_ID": os.getenv("IPAT_INET_ID", ""),
            "IPAT_USER_NUMBER": os.getenv("IPAT_USER_NUMBER", ""),
            "IPAT_PASSWORD": os.getenv("IPAT_PASSWORD", ""),
            "IPAT_P_ARS": os.getenv("IPAT_P_ARS", ""),
        }
        missing = [key for key, value in values.items() if not value]
        if missing:
            raise ValueError(f"環境変数が未設定です: {', '.join(missing)} (.env を確認してください)")
        return cls(
            inet_id=values["IPAT_INET_ID"],
            user_number=values["IPAT_USER_NUMBER"],
            password=values["IPAT_PASSWORD"],
            p_ars=values["IPAT_P_ARS"],
        )


JEV_API_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"


def jev_api_key() -> str:
    """環境変数 TYPESAFE_API_KEY(.env 可)から Jev の API キーを取得する."""
    key = os.getenv("TYPESAFE_API_KEY", "").strip()
    if not key:
        raise ValueError("環境変数 TYPESAFE_API_KEY が未設定です (.env を確認してください)")
    return key


@dataclass
class JevConfig:
    """Jev による購入判定のパラメータ.

    Jev(TypeSafe AI の決定専用モデル)は数値計算・大小比較が苦手なため、勝率・変化率・人気順位は
    コードで計算して State に含め、Jev には「どの馬が最も勝ちそうか」の選択(Choice)だけを任せる。
    購入可否の最終判定(下の閾値)もコード側で行う。
    """

    model: str = JEV_MODEL
    timeout_seconds: float = 10.0
    max_attempts: int = 3  # 429/529/5xx/通信エラー時の試行回数

    # 30分前(または10分前)→4分前でオッズが何 % 動いたら 売れた(shortening)/離れた(drifting) とみなすか。
    # 旧実装 monitor_pre_race_odds.py の judge_movement と同じ 10%。
    trend_pct: float = 10.0

    # 購入条件(すべて満たしたときだけ購入)
    min_confidence: float = 0.5  # Jev の確信度の下限。公式ドキュメントの「慎重に動く」境界
    min_win_prob: float = 0.25  # Jev が返した選択確率(=勝率)の下限
    min_odds: float = 1.5  # 4分前単勝オッズの下限(元返し級を避ける)
    max_odds: float = 10.0  # 4分前単勝オッズの上限
    min_ev: float = 1.0  # 期待値(Jev の勝率 × 4分前オッズ)の下限。1.0 = 収支トントン
