"""Jev による候補の選択。

**入力が何件あっても、この層が投げるリクエストは 1 回。** N 件の入力を ``state``
に並べ、質問を N 個並列に置く。公式クックブックで 12.2 倍安・10.0 倍速の実績が
あるパターンで、1 件ずつ呼ぶ実装にしてはならない。

このモジュールは候補と :class:`Decision` の対応づけまでを担い、**閾値との比較は
しない**。判断は :mod:`jev_abr_geocoder.geocoder` が :mod:`config` の閾値を見て行う。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..config import (
    BEAM_QUESTION,
    NONE_OPTION,
    NONE_OPTION_DESCRIPTION,
    NUMBER_QUESTION,
    TOWN_QUESTION,
    GeocoderConfig,
)
from ..models import Decision, NumberKind, Usage

__all__ = [
    "DecisionModel",
    "JevModel",
    "Reranker",
    "TownAsk",
    "NumberAsk",
    "BeamAsk",
    "BeamOutcome",
    "RerankOutcome",
]

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


@dataclass(frozen=True, slots=True)
class BeamAsk:
    """候補が Choice の上限を超えたときの分割絞り込みの依頼。"""

    query: str
    normalized: str
    #: 市区町村配下の全町字。索引の順と 1 対 1 で対応する。
    options: Sequence[str]


@dataclass(slots=True)
class BeamOutcome:
    """各依頼について、勝ち残ったオプションの添字。"""

    survivors: list[list[int]]
    usage: Usage
    failure: str = ""


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

    async def narrow(self, asks: Sequence[BeamAsk]) -> BeamOutcome:
        """候補を Choice の上限以下に絞る。

        一覧を上限ごとに分割し、各分割について「この一覧の中にあるか」を訊く。
        Jev は 1 リクエスト内の全質問を並列に評価するので、分割を詰め込めるだけ
        往復は減る。

        ただし **Jev の上限は 64k tokens/リクエスト**で、実測では 254 件の
        選択肢 1 分割が 9,727 トークン。超えると max_tokens_exceeded で
        リクエストごと失敗するので、トークン量を見積もって
        ``beam_token_budget`` に収まるように詰め、**並行に**投げる。

        絞り込みの基準はここでも設けない。どれを残すかは Jev が決める。
        """
        if not asks:
            return BeamOutcome(survivors=[], usage=Usage())

        size = self._cfg.max_candidates
        # (ask の添字, 分割番号, 選択肢) を平らに並べてから詰める。
        jobs: list[tuple[int, int, Sequence[str]]] = []
        for index, ask in enumerate(asks):
            for chunk_no, start in enumerate(range(0, len(ask.options), size)):
                jobs.append((index, chunk_no, ask.options[start : start + size]))

        groups = _pack(jobs, self._cfg.beam_token_budget)
        results = await asyncio.gather(*(self._narrow_group(asks, group) for group in groups))

        survivors: list[list[int]] = [[] for _ in asks]
        usage = Usage()
        failure = ""
        for picked, group_usage, group_failure in results:
            _add_usage(usage, group_usage)
            failure = failure or group_failure
            for ask_index, option_index in picked:
                survivors[ask_index].append(option_index)
        for kept in survivors:
            kept.sort()
        return BeamOutcome(survivors=survivors, usage=usage, failure=failure)

    async def _narrow_group(
        self, asks: Sequence[BeamAsk], group: Sequence[tuple[int, int, Sequence[str]]]
    ) -> tuple[list[tuple[int, int]], Usage, str]:
        size = self._cfg.max_candidates
        state: dict[str, Any] = {}
        questions: dict[str, Any] = {}
        for ask_index, chunk_no, options in group:
            key = f"q{ask_index}"
            if key not in state:
                ask = asks[ask_index]
                state[key] = {"入力": ask.query, "正規化": ask.normalized}
            questions[f"{key}_{chunk_no}"] = _choice(
                instructions={"対象": f"`{key}`", "質問": BEAM_QUESTION},
                options=options,
                label="住所",
            )

        usage = Usage()
        try:
            answers, tokens = await self._model.ask(state, questions)
        except Exception as exc:  # noqa: BLE001 - 外部モデルの不調で落とさない
            _log.warning("Jev の分割絞り込みに失敗: %s", exc)
            return [], usage, str(exc)
        usage.add(tokens[0], tokens[1])

        picked: list[tuple[int, int]] = []
        for ask_index, chunk_no, _options in group:
            decision = _decision(answers.get(f"q{ask_index}_{chunk_no}"))
            if decision.index is not None:
                picked.append((ask_index, chunk_no * size + decision.index))
        return picked, usage, ""

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


#: 1 選択肢あたりの固定費（JSON の鍵と区切り）と、日本語 1 文字あたりの
#: トークン数。実測（254 件・4,211 文字で 9,727 トークン）から取った。
_TOKENS_PER_CHAR = 2.0
_TOKENS_PER_OPTION = 6
#: 質問文と state の分。
_REQUEST_OVERHEAD = 800


def _estimate_tokens(options: Sequence[str]) -> int:
    chars = sum(len(option) for option in options)
    return int(chars * _TOKENS_PER_CHAR) + len(options) * _TOKENS_PER_OPTION


def _pack(
    jobs: Sequence[tuple[int, int, Sequence[str]]], budget: int
) -> list[list[tuple[int, int, Sequence[str]]]]:
    """見積もりトークン量が ``budget`` に収まるように分割をまとめる。"""
    groups: list[list[tuple[int, int, Sequence[str]]]] = []
    current: list[tuple[int, int, Sequence[str]]] = []
    used = _REQUEST_OVERHEAD
    for job in jobs:
        cost = _estimate_tokens(job[2])
        if current and used + cost > budget:
            groups.append(current)
            current = []
            used = _REQUEST_OVERHEAD
        current.append(job)
        used += cost
    if current:
        groups.append(current)
    return groups


def _add_usage(target: Usage, source: Usage) -> None:
    target.input_tokens += source.input_tokens
    target.output_tokens += source.output_tokens
    target.requests += source.requests


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
