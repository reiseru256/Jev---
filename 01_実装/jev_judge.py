"""Jev による購入判定.

30分前 / 10分前 / 4分前の単勝オッズから、Jev(TypeSafe AI の決定専用モデル)に
「このレースで最も勝ちそうな馬」を選ばせ、条件を満たすときだけ購入対象にする。

役割分担(Jev は数値計算・大小比較が苦手 = https://docs.typesafe.ai/model-jaggedness/jev-1.13.md):
  * コード: 暗黙勝率(1/オッズの正規化)・オッズ変化率・人気順位・売れた/離れた の判定を計算する
  * Jev  : 計算済みの表を読み、勝ちそうな馬を Choice で 1 頭選ぶ(各馬の確率と確信度も返る)
  * コード: Jev の答えが購入条件(確信度・勝率・オッズ帯・期待値・オッズ上昇)を満たすか最終判定する

Jev は英語が主で、無関係な情報が精度を下げるため、State は英語・馬番のみ(馬名なし)・必要な列だけにしている。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol

from config import JevConfig
from jev_client import ChoiceAnswer, JevError

# 馬番 -> (馬名, 単勝オッズ)
Snapshot = dict[int, tuple[str, float]]

# 早い時点から順に。表の列名と対応する
SNAPSHOT_ORDER = ("30m", "10m", "4m")
LABEL_TEXT = {"30m": "30min", "10m": "10min", "4m": "4min"}
QUESTION_NAME = "winner"

INSTRUCTIONS = (
    "Choose the single horse most likely to win this race, using only the runners table. "
    "implied_win_prob_4min_pct is the market's win probability at 4 minutes before the start. "
    "Prefer a horse with a high implied_win_prob_4min_pct whose market_trend is 'shortening' "
    "(win odds fell, meaning money is coming in). "
    "Never choose a horse whose market_trend is 'drifting' (win odds rose, meaning it is being sold). "
    "market_trend 'unknown' means no earlier odds are available; treat it as neutral."
)


class ChoiceClient(Protocol):
    def ask_choice(self, state, name: str, instructions: str, criteria: dict[str, str]) -> ChoiceAnswer: ...


@dataclass
class HorseEstimate:
    number: int
    name: str
    odds: float  # 4分前の単勝オッズ
    win_prob: float  # Jev が返した選択確率(=勝率)
    confidence: float  # Jev の確信度
    trend: str  # shortening / drifting / stable / unknown

    @property
    def ev(self) -> float:
        """期待値 = 勝率 × オッズ。1.0 で収支トントン."""
        return self.win_prob * self.odds


@dataclass
class JevDecision:
    buy: bool
    reason: str
    horse: HorseEstimate | None = None
    ranking: list[HorseEstimate] = field(default_factory=list)
    error: bool = False  # Jev API の失敗。再試行の余地があるときに呼び出し側が使う


def implied_probs(snapshot: Snapshot) -> dict[int, float]:
    """1/オッズ を合計 1 に正規化した市場の勝率(控除率を除く)."""
    raw = {no: 1.0 / odds for no, (_, odds) in snapshot.items() if odds and odds > 0 and math.isfinite(odds)}
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {no: value / total for no, value in raw.items()}


def _trend(first: float | None, last: float, threshold_pct: float) -> tuple[str, float | None]:
    if first is None or first <= 0:
        return "unknown", None
    change_pct = (last / first - 1.0) * 100.0
    if change_pct <= -threshold_pct:
        return "shortening", change_pct
    if change_pct >= threshold_pct:
        return "drifting", change_pct
    return "stable", change_pct


def build_state(snapshots: dict[str, Snapshot], config: JevConfig) -> tuple[dict, dict[int, str]]:
    """Jev に渡す State と、馬番 -> market_trend を作る."""
    latest = snapshots["4m"]
    probs = implied_probs(latest)
    ranked = sorted(probs, key=lambda no: probs[no], reverse=True)
    popularity = {no: rank for rank, no in enumerate(ranked, start=1)}

    earlier = [label for label in SNAPSHOT_ORDER[:-1] if snapshots.get(label)]
    trends: dict[int, str] = {}
    runners = []
    for no in sorted(probs):
        row: dict[str, object] = {"horse_no": no}
        for label in earlier:
            if no in snapshots[label]:
                row[f"win_odds_{LABEL_TEXT[label]}_before"] = snapshots[label][no][1]
        row["win_odds_4min_before"] = latest[no][1]
        row["implied_win_prob_4min_pct"] = round(probs[no] * 100, 1)
        row["popularity_rank_4min"] = popularity[no]

        first = next((snapshots[label][no][1] for label in earlier if no in snapshots[label]), None)
        trend, change_pct = _trend(first, latest[no][1], config.trend_pct)
        if change_pct is not None:
            row["odds_change_pct_first_to_4min"] = round(change_pct, 1)
        row["market_trend"] = trend
        trends[no] = trend
        runners.append(row)

    state = {
        "race": {
            "runners": len(runners),
            "odds_snapshots_minutes_before_start": [int(LABEL_TEXT[l].removesuffix("min")) for l in SNAPSHOT_ORDER if snapshots.get(l)],
        },
        "runners": runners,
    }
    return state, trends


def judge(snapshots: dict[str, Snapshot], client: ChoiceClient, config: JevConfig | None = None) -> JevDecision:
    """購入判定を行う。4分前のスナップショットが必須."""
    config = config or JevConfig()
    latest = snapshots.get("4m")
    if not latest:
        return JevDecision(False, "4分前オッズが未取得")
    if len(implied_probs(latest)) < 2:
        return JevDecision(False, "有効なオッズが2頭分に満たない")

    state, trends = build_state(snapshots, config)
    criteria = {f"h{no}": f"Horse number {no} wins the race" for no in trends}
    try:
        answer = client.ask_choice(state, QUESTION_NAME, INSTRUCTIONS, criteria)
    except JevError as exc:
        return JevDecision(False, f"Jev API エラー: {exc}", error=True)

    def estimate(key: str) -> HorseEstimate:
        no = int(key.removeprefix("h"))
        return HorseEstimate(
            number=no,
            name=latest[no][0],
            odds=latest[no][1],
            win_prob=answer.probabilities.get(key, 0.0),
            confidence=answer.confidence,
            trend=trends[no],
        )

    ranking = sorted((estimate(key) for key in criteria), key=lambda h: h.win_prob, reverse=True)
    top = estimate(answer.choice)

    checks = [
        (top.trend != "drifting", "オッズ上昇(売られている)"),
        (top.confidence >= config.min_confidence, f"確信度 {top.confidence:.2f} < {config.min_confidence}"),
        (top.win_prob >= config.min_win_prob, f"Jev勝率 {top.win_prob:.1%} < {config.min_win_prob:.1%}"),
        (top.odds >= config.min_odds, f"オッズ {top.odds} < {config.min_odds}"),
        (top.odds <= config.max_odds, f"オッズ {top.odds} > {config.max_odds}"),
        (top.ev >= config.min_ev, f"期待値 {top.ev:.3f} < {config.min_ev}"),
    ]
    failed = [message for ok, message in checks if not ok]
    if failed:
        return JevDecision(False, f"見送り: {top.number}番 " + " / ".join(failed), top, ranking)

    reason = (
        f"購入: {top.number}番 Jev勝率 {top.win_prob:.1%} 確信度 {top.confidence:.2f} "
        f"期待値 {top.ev:.3f} ({top.trend})"
    )
    return JevDecision(True, reason, top, ranking)
