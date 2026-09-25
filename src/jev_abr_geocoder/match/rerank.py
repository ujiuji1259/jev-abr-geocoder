"""Jev による候補の選択。

**入力が何件あっても、この層が投げるリクエストは 1 回。** N 件の入力を ``state``
に並べ、質問を N 個並列に置く。公式クックブックで 12.2 倍安・10.0 倍速の実績が
あるパターンで、1 件ずつ呼ぶ実装にしてはならない。

このモジュールは候補と :class:`Decision` の対応づけまでを担い、**閾値との比較は
しない**。判断は :mod:`jev_abr_geocoder.geocoder` が :mod:`config` の閾値を見て行う。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..config import (
    NONE_OPTION,
    NONE_OPTION_DESCRIPTION,
    NUMBER_QUESTION,
    TOWN_QUESTION,
    GeocoderConfig,
)
from ..models import Decision, NumberKind, Usage

__all__ = ["DecisionModel", "JevModel", "Reranker", "TownAsk", "NumberAsk", "RerankOutcome"]

_log = logging.getLogger(__name__)


class DecisionModel(Protocol):
    """Jev の呼び出し口。

    Protocol にしてあるのは、テストで差し替えられるようにするため。これが無いと
    Jev 無しでは何も検証できなくなる。
    """

    async def ask(
        self, state: Any, questions: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], tuple[int, int]]:
        """``(answers, (input_tokens, output_tokens))`` を返す。"""
        ...


@dataclass(frozen=True, slots=True)
class TownAsk:
    """1 件分の町字選択の依頼。"""

    query: str
    normalized: str
    #: 選択肢の表示住所。索引の順と 1 対 1 で対応する。
    options: Sequence[str]


@dataclass(frozen=True, slots=True)
class NumberAsk:
    """1 件分の番号選択の依頼。"""

    query: str
    #: 確定済みの町字（表示表記）
    town: str
    #: 町字より後ろの入力（建物名を含んだまま）
    tail: str
    kind: NumberKind
    #: 選択肢の番号表記。索引の順と 1 対 1 で対応する。
    options: Sequence[str]


@dataclass(slots=True)
class RerankOutcome:
    decisions: list[Decision]
    usage: Usage
    #: Jev を呼べなかった場合の理由。空なら正常。
    failure: str = ""


class JevModel:
    """``typesafe-sdk`` の薄いラッパ。"""

    def __init__(self, client: Any, model: str, timeout: float) -> None:
        self._client = client
        self._model = model
        self._timeout = timeout

    @classmethod
    def from_env(cls, cfg: GeocoderConfig) -> JevModel:
        """``TYPESAFE_API_KEY`` から非同期クライアントを作る。"""
        from typesafe_sdk import AsyncTypeSafeClient

        return cls(AsyncTypeSafeClient(), cfg.model, cfg.timeout)

    async def ask(
        self, state: Any, questions: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], tuple[int, int]]:
        response = await self._client.system_one(
            state=state, questions=questions, model=self._model, timeout=self._timeout
        )
        usage = getattr(response, "usage", None)
        tokens = (
            (getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0)
            if usage is not None
            else (0, 0)
        )
        return response.answers, tokens


class Reranker:
    def __init__(self, model: DecisionModel, cfg: GeocoderConfig) -> None:
        self._model = model
        self._cfg = cfg

    async def pick_towns(self, asks: Sequence[TownAsk]) -> RerankOutcome:
        if not asks:
            return RerankOutcome(decisions=[], usage=Usage())
        state: dict[str, Any] = {}
        questions: dict[str, Any] = {}
        for index, ask in enumerate(asks):
            key = f"q{index}"
            state[key] = {"入力": ask.query, "正規化": ask.normalized}
            questions[key] = _choice(
                instructions={"対象": f"`{key}`", "質問": TOWN_QUESTION},
                options=ask.options,
                label="住所",
            )
        return await self._run(state, questions, len(asks))

    async def pick_numbers(self, asks: Sequence[NumberAsk]) -> RerankOutcome:
        if not asks:
            return RerankOutcome(decisions=[], usage=Usage())
        state: dict[str, Any] = {}
        questions: dict[str, Any] = {}
        for index, ask in enumerate(asks):
            key = f"q{index}"
            state[key] = {"入力": ask.query, "町字": ask.town, "町字より後ろ": ask.tail}
            questions[key] = _choice(
                instructions={
                    "対象": f"`{key}`",
                    "町字": f"`{key}.町字`",
                    "質問": NUMBER_QUESTION,
                },
                options=ask.options,
                label="番号",
            )
        return await self._run(state, questions, len(asks))

    async def _run(
        self, state: Mapping[str, Any], questions: Mapping[str, Any], count: int
    ) -> RerankOutcome:
        usage = Usage()
        try:
            answers, tokens = await self._model.ask(state, questions)
        except Exception as exc:  # noqa: BLE001 - 外部モデルの不調で落とさない
            # API サーバとして 500 を返さないために、ここで握って呼び出し側に
            # 語彙スコア最上位へフォールバックさせる。
            _log.warning("Jev の呼び出しに失敗: %s", exc)
            return RerankOutcome(decisions=[], usage=usage, failure=str(exc))
        usage.add(tokens[0], tokens[1])
        return RerankOutcome(
            decisions=[_decision(answers.get(f"q{i}")) for i in range(count)], usage=usage
        )


#: Jev の Choice が受け付けるオプション数の上限（API の制約）。
MAX_CHOICE_OPTIONS = 255


def _choice(instructions: Mapping[str, Any], options: Sequence[str], label: str) -> Any:
    from typesafe_sdk import Choice

    # NONE_OPTION のぶん 1 枠を残す。呼び出し側が守っているはずだが、
    # 超えると API が 400 を返して**バッチ全体が失敗する**ので、ここでも守る。
    capped = options[: MAX_CHOICE_OPTIONS - 1]
    criteria: dict[str, Any] = {_option_id(i): {label: text} for i, text in enumerate(capped)}
    criteria[NONE_OPTION] = NONE_OPTION_DESCRIPTION
    return Choice(instructions=dict(instructions), criteria=criteria)


def _option_id(index: int) -> str:
    return f"c{index}"


def _decision(answer: Any) -> Decision:
    """Choice の答えを :class:`Decision` に直す。閾値との比較はしない。"""
    if answer is None:
        return Decision(index=None, probability=0.0, confidence=0.0, contains_answer=0.0)
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
