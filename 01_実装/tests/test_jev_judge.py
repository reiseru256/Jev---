import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from config import JevConfig
from fakes import FakeJev
from jev_client import JevError
from jev_judge import build_state, implied_probs, judge

TAIL = (15.0, 20.0, 30.0, 40.0, 60.0, 80.0)


def field(*head):
    """6〜11番人気を足して 1/オッズ の合計を現実的(約1.2)にした 11 頭立て."""
    return {i + 1: (f"馬{i + 1}", o) for i, o in enumerate((*head, *TAIL))}


# 1番人気が 3.6倍 → 2.5倍 と売れていく
FLOW = {
    "30m": field(3.6, 4.0, 6.0, 9.0, 12.0),
    "10m": field(3.0, 4.2, 6.4, 9.0, 12.5),
    "4m": field(2.5, 4.5, 6.8, 9.5, 13.0),
}


def test_implied_probs_sum_to_one():
    probs = implied_probs(FLOW["4m"])
    assert sum(probs.values()) == pytest.approx(1.0)
    assert probs[1] > probs[2] > probs[3]


def test_state_is_english_numbers_only_with_precomputed_features():
    state, trends = build_state(FLOW, JevConfig())
    assert state["race"]["runners"] == 11
    assert state["race"]["odds_snapshots_minutes_before_start"] == [30, 10, 4]
    first = state["runners"][0]
    assert first["horse_no"] == 1
    assert first["win_odds_30min_before"] == 3.6
    assert first["win_odds_10min_before"] == 3.0
    assert first["win_odds_4min_before"] == 2.5
    assert first["popularity_rank_4min"] == 1
    assert first["odds_change_pct_first_to_4min"] == pytest.approx(-30.6)
    assert first["market_trend"] == "shortening"
    assert "name" not in first and "馬1" not in str(state)  # 馬名は渡さない(英語・必要な列だけ)
    # 2番 4.0→4.5 (+12.5%) は離れた、4番 9.0→9.5 (+5.6%) は小動き
    assert trends[1] == "shortening" and trends[2] == "drifting" and trends[4] == "stable"


def test_trend_drifting_and_unknown():
    snaps = {"30m": field(2.2, 4.0, 6.0, 9.0, 12.0), "4m": field(3.2, 3.8, 6.0, 9.0, 12.0)}
    _, trends = build_state(snaps, JevConfig())
    assert trends[1] == "drifting"
    _, trends = build_state({"4m": field(2.5, 4.5, 6.8, 9.5, 13.0)}, JevConfig())
    assert set(trends.values()) == {"unknown"}


def test_buys_the_horse_jev_picks():
    jev = FakeJev(pick=1, prob=0.42, confidence=0.9)
    decision = judge(FLOW, jev)
    assert decision.buy, decision.reason
    assert decision.horse.number == 1 and decision.horse.odds == 2.5
    assert decision.horse.ev == pytest.approx(0.42 * 2.5)
    state, name, instructions, criteria = jev.calls[0]
    assert name == "winner" and "drifting" in instructions
    assert set(criteria) == {f"h{n}" for n in range(1, 12)}


def test_missing_4m_is_not_bought_and_jev_not_called():
    jev = FakeJev()
    decision = judge({"30m": FLOW["30m"]}, jev)
    assert not decision.buy and "4分前" in decision.reason
    assert jev.calls == []


@pytest.mark.parametrize(
    "kwargs, config, expected",
    [
        (dict(confidence=0.3), JevConfig(), "確信度"),
        (dict(prob=0.10), JevConfig(min_ev=0.0), "Jev勝率"),
        (dict(prob=0.30), JevConfig(), "期待値"),  # 0.30 * 2.5 = 0.75 < 1.0
        (dict(prob=0.60), JevConfig(min_odds=3.0, min_ev=0.0), "オッズ"),
        (dict(prob=0.60), JevConfig(max_odds=2.0, min_ev=0.0), "オッズ"),
    ],
)
def test_thresholds_block_purchase(kwargs, config, expected):
    decision = judge(FLOW, FakeJev(pick=1, **kwargs), config)
    assert not decision.buy
    assert expected in decision.reason


def test_drifting_horse_is_never_bought_even_if_jev_picks_it():
    snaps = {"30m": field(2.2, 4.0, 6.0, 9.0, 12.0), "4m": field(3.2, 3.8, 6.0, 9.0, 12.0)}
    decision = judge(snaps, FakeJev(pick=1, prob=0.9), JevConfig(min_ev=0.0))
    assert not decision.buy and "オッズ上昇" in decision.reason


def test_api_error_is_a_retryable_skip():
    decision = judge(FLOW, FakeJev(error=JevError("HTTP 529")))
    assert not decision.buy and decision.error
    assert "Jev API エラー" in decision.reason
