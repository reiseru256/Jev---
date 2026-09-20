"""テスト用の偽 Jev クライアント."""

from jev_client import ChoiceAnswer


class FakeJev:
    """Jev API の代わり。選ぶ馬・確率・確信度を固定で返し、受け取った内容を記録する."""

    def __init__(self, pick=1, prob=0.40, confidence=0.9, error=None):
        self.pick, self.prob, self.confidence, self.error = pick, prob, confidence, error
        self.calls = []

    def ask_choice(self, state, name, instructions, criteria):
        self.calls.append((state, name, instructions, criteria))
        if self.error:
            raise self.error
        rest = (1.0 - self.prob) / (len(criteria) - 1)
        probs = {key: (self.prob if key == f"h{self.pick}" else rest) for key in criteria}
        return ChoiceAnswer(f"h{self.pick}", self.confidence, probs)
