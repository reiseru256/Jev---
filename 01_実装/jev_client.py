"""Jev(TypeSafe AI)API クライアント.

Jev は文章を生成せず、Choice(選択)・Score(採点)・Noul(真偽の確率)だけを返す決定専用モデル。
ここでは購入判定に使う Choice だけを扱う。

    POST https://api.typesafe.ai/v1/systemone
    Authorization: Bearer <TYPESAFE_API_KEY>
    {"state": ..., "model": "jev-latest", "questions": {name: {"type": "choice", ...}}}

公式 Python SDK(typesafe-sdk)は使わず requests で直接呼ぶ(依存を増やさないため)。
仕様: https://docs.typesafe.ai/api.md
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from config import JEV_API_URL, JEV_MODEL

logger = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504, 529}  # 429/529 は公式が指数バックオフを推奨


class JevError(Exception):
    """Jev API の呼び出し失敗(通信・認証・入力不正・応答の形式不正)."""


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    confidence: float
    probabilities: dict[str, float]


class JevClient:
    def __init__(
        self,
        api_key: str,
        model: str = JEV_MODEL,
        url: str = JEV_API_URL,
        timeout: float = 10.0,
        max_attempts: int = 3,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._url = url
        self._timeout = timeout
        self._max_attempts = max(1, max_attempts)
        self._session = session or requests.Session()
        self._sleep = sleep

    def ask_choice(
        self,
        state: Any,
        name: str,
        instructions: str,
        criteria: dict[str, str],
    ) -> ChoiceAnswer:
        """state に対する Choice 質問を 1 つ投げ、選択と各選択肢の確率を返す."""
        payload = {
            "state": state,
            "model": self.model,
            "questions": {name: {"type": "choice", "instructions": instructions, "criteria": criteria}},
        }
        body = self._post(payload)
        answer = (body.get("answers") or {}).get(name)
        if not isinstance(answer, dict):
            raise JevError(f"応答に '{name}' の回答がありません")
        try:
            choice = str(answer["choice"])
            confidence = float(answer["confidence"])
            probabilities = {str(k): float(v) for k, v in (answer.get("probabilities") or {}).items()}
        except (KeyError, TypeError, ValueError) as exc:
            raise JevError(f"応答の形式が不正です: {answer!r}") from exc
        if choice not in criteria:
            raise JevError(f"criteria に無い選択が返りました: {choice!r}")
        return ChoiceAnswer(choice, confidence, probabilities)

    def _post(self, payload: dict) -> dict:
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        last_error = "不明なエラー"
        for attempt in range(self._max_attempts):
            try:
                response = self._session.post(self._url, json=payload, headers=headers, timeout=self._timeout)
            except requests.RequestException as exc:
                last_error = f"通信エラー: {exc}"
            else:
                status = response.status_code
                if status == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise JevError("応答が JSON ではありません") from exc
                if status == 401:
                    raise JevError("認証に失敗しました(TYPESAFE_API_KEY を確認してください)")
                if status not in RETRY_STATUS:
                    raise JevError(f"HTTP {status}: {response.text[:300]}")
                last_error = f"HTTP {status}"

            if attempt < self._max_attempts - 1:
                delay = 0.5 * (2**attempt)
                logger.warning("Jev API %s。%.1f 秒後に再試行 (%d/%d)", last_error, delay, attempt + 1, self._max_attempts)
                self._sleep(delay)
        raise JevError(f"{last_error}({self._max_attempts} 回試行)")
