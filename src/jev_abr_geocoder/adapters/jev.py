"""Jev (TypeSafe System One) を :class:`ports.DecisionModel` に合わせる。

**``typesafe-sdk`` を import していいのはこのファイルだけ。** Choice の組み立て、
「候補のいずれでもない」の予約オプション、オプション ID の符号化、255 件の
上限、応答の確率分布の読み取りは、すべてこの壁の内側にある。コアは
:class:`ports.Question` を並べて :class:`ports.Decision` を受け取るだけ。

**1 リクエストで全問に答える。** N 件の入力を ``state`` に並べ、質問を N 個
並列に置く。公式クックブックで 12.2 倍安・10.0 倍速の実績があるパターンで、
1 件ずつ呼ぶ実装にしてはならない（docs/code-design.md 制約1）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .. import ports
from ..config import NONE_OPTION, NONE_OPTION_DESCRIPTION, GeocoderConfig
from ..decision import Decision, Usage

__all__ = ["JevModel", "MAX_CHOICE_OPTIONS"]

#: Jev の Choice が受け付けるオプション数の上限（API の制約）。
#: 「該当なし」もこの数に含まれる。``GeocoderConfig.max_options`` は
#: これ以下であることを自身で検証する。
MAX_CHOICE_OPTIONS = 255


class JevModel:
    """:class:`ports.DecisionModel` の typesafe-sdk 実装。"""

    def __init__(self, client: Any, model: str, timeout: float) -> None:
        self._client = client
        self._model = model
        self._timeout = timeout

    @classmethod
    def from_env(cls, cfg: GeocoderConfig) -> JevModel:
        """``TYPESAFE_API_KEY`` から非同期クライアントを作る。"""
        from typesafe_sdk import AsyncTypeSafeClient

        return cls(AsyncTypeSafeClient(), cfg.model, cfg.timeout)

    async def choose(self, questions: Sequence[ports.Question]) -> ports.Answers:
        if not questions:
            return ports.Answers(decisions=[], usage=Usage())

        state, payload = _build(questions)
        try:
            response = await self._client.system_one(
                state=state, questions=payload, model=self._model, timeout=self._timeout
            )
            answers: Mapping[str, Any] = response.answers
        except Exception as exc:  # noqa: BLE001 - ベンダの例外をポートの語彙に直す
            raise ports.ModelUnavailable(str(exc)) from exc

        usage = Usage()
        usage.add(*_tokens(response))
        return ports.Answers(
            decisions=[_decision(answers.get(_question_id(i))) for i in range(len(questions))],
            usage=usage,
        )


def _build(questions: Sequence[ports.Question]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(state, questions)`` を組む。

    **同じ材料は ``state`` に 1 つしか置かない。** 分割絞り込み は同じ
    入力について何十問も並べるので、材料を問ごとに複製するとトークンが嵩む。
    """
    from typesafe_sdk import Choice

    state: dict[str, Any] = {}
    payload: dict[str, Any] = {}
    subject_keys: dict[tuple[tuple[str, str], ...], str] = {}

    for index, question in enumerate(questions):
        fingerprint = tuple(question.subject.items())
        key = subject_keys.get(fingerprint)
        if key is None:
            key = f"s{len(state)}"
            state[key] = dict(question.subject)
            subject_keys[fingerprint] = key

        instructions: dict[str, Any] = {"対象": f"`{key}`"}
        for label in question.refers_to:
            instructions[label] = f"`{key}.{label}`"
        instructions["質問"] = question.question

        # NONE_OPTION のぶん 1 枠を残す。呼び出し側が守っているはずだが、
        # 超えると API が 400 を返して**バッチ全体が失敗する**ので、ここでも守る。
        capped = list(question.options)[: MAX_CHOICE_OPTIONS - 1]
        criteria: dict[str, Any] = {
            _option_id(i): {question.label: text} for i, text in enumerate(capped)
        }
        criteria[NONE_OPTION] = NONE_OPTION_DESCRIPTION
        payload[_question_id(index)] = Choice(instructions=instructions, criteria=criteria)

    return state, payload


def _question_id(index: int) -> str:
    return f"a{index}"


def _option_id(index: int) -> str:
    return f"c{index}"


def _tokens(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    return (
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )


def _decision(answer: Any) -> Decision:
    """Choice の答えを :class:`Decision` に直す。閾値との比較はしない。"""
    if answer is None:
        return Decision.unanswered()
    choice = getattr(answer, "choice", None)
    probabilities: Mapping[str, float] = getattr(answer, "probabilities", {}) or {}
    confidence = float(getattr(answer, "confidence", 0.0) or 0.0)
    none_mass = float(probabilities.get(NONE_OPTION, 0.0))
    contains_answer = max(0.0, 1.0 - none_mass)

    if not choice or choice == NONE_OPTION:
        return Decision(
            index=None,
            probability=none_mass,
            confidence=confidence,
            contains_answer=contains_answer,
        )
    try:
        index = int(str(choice)[1:])
    except ValueError:
        return Decision(
            index=None, probability=0.0, confidence=confidence, contains_answer=contains_answer
        )
    return Decision(
        index=index,
        probability=float(probabilities.get(str(choice), 0.0)),
        confidence=confidence,
        contains_answer=contains_answer,
    )
