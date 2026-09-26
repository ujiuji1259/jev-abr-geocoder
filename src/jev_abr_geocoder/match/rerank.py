"""判定モデルへの問いの組み立てと、答えの解釈。

**入力が何件あっても、この層が投げるリクエストは 1 回。** N 件分の問いを
1 つの :meth:`ports.DecisionModel.choose` に渡す。1 件ずつ呼ぶ実装にしては
ならない（公式クックブックで 12.2 倍安・10.0 倍速の実績があるパターン）。

このモジュールは候補と :class:`Decision` の対応づけまでを担い、**閾値との比較は
しない**。判断は :mod:`jev_abr_geocoder.geocoder` が :mod:`config` の閾値を見て行う。

モデルの実装は知らない（:class:`ports.DecisionModel` だけを見る）。Choice の
作り方も確率分布の読み方も ``adapters/jev.py`` の側にあるので、ここに書くのは
**どの候補をどう並べて訊くか**という段取りだけ。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from .. import ports
from ..config import (
    ADDRESS_LABEL,
    BEAM_QUESTION,
    NUMBER_LABEL,
    NUMBER_QUESTION,
    TOWN_QUESTION,
    GeocoderConfig,
)
from ..models import Decision, NumberKind, Usage

__all__ = [
    "Reranker",
    "TownAsk",
    "NumberAsk",
    "BeamAsk",
    "BeamOutcome",
    "RerankOutcome",
]

_log = logging.getLogger(__name__)


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
    #: モデルを呼べなかった場合の理由。空なら正常。
    failure: str = ""


class Reranker:
    def __init__(self, model: ports.DecisionModel, cfg: GeocoderConfig) -> None:
        self._model = model
        self._cfg = cfg

    async def pick_towns(self, asks: Sequence[TownAsk]) -> RerankOutcome:
        if not asks:
            return RerankOutcome(decisions=[], usage=Usage())
        questions = [
            ports.Question(
                subject={"入力": ask.query, "正規化": ask.normalized},
                question=TOWN_QUESTION,
                options=ask.options,
                label=ADDRESS_LABEL,
            )
            for ask in asks
        ]
        return await self._run(questions)

    async def pick_numbers(self, asks: Sequence[NumberAsk]) -> RerankOutcome:
        if not asks:
            return RerankOutcome(decisions=[], usage=Usage())
        questions = [
            ports.Question(
                subject={"入力": ask.query, "町字": ask.town, "町字より後ろ": ask.tail},
                question=NUMBER_QUESTION,
                options=ask.options,
                label=NUMBER_LABEL,
                cite=("町字",),
            )
            for ask in asks
        ]
        return await self._run(questions)

    async def narrow(self, asks: Sequence[BeamAsk]) -> BeamOutcome:
        """候補を Choice の上限以下に絞る。

        一覧を上限ごとに分割し、各分割について「この一覧の中にあるか」を訊く。
        判定モデルは 1 リクエスト内の全問を並列に評価するので、分割を詰め込める
        だけ往復は減る。

        ただし **上限は 64k tokens/リクエスト**で、実測では 254 件の選択肢
        1 分割が 9,727 トークン。超えるとリクエストごと失敗するので、トークン量を
        見積もって ``beam_token_budget`` に収まるように詰め、**並行に**投げる。

        絞り込みの基準はここでも設けない。どれを残すかはモデルが決める。
        """
        if not asks:
            return BeamOutcome(survivors=[], usage=Usage())

        size = self._cfg.max_candidates
        # (ask の添字, 分割番号, 選択肢) を平らに並べてから詰める。
        jobs: list[tuple[int, int, Sequence[str]]] = []
        for index, ask in enumerate(asks):
            for chunk_no, start in enumerate(range(0, len(ask.options), size)):
                jobs.append((index, chunk_no, ask.options[start : start + size]))

        groups = _pack(jobs, self._cfg.beam_token_budget, self._cfg.beam_max_asks_per_request)
        results = await asyncio.gather(*(self._narrow_group(asks, group) for group in groups))

        survivors: list[list[int]] = [[] for _ in asks]
        usage = Usage()
        failure = ""
        for picked, group_usage, group_failure in results:
            usage.merge(group_usage)
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
        questions = [
            ports.Question(
                subject={"入力": asks[ask_index].query, "正規化": asks[ask_index].normalized},
                question=BEAM_QUESTION,
                options=options,
                label=ADDRESS_LABEL,
            )
            for ask_index, _chunk_no, options in group
        ]
        outcome = await self._run(questions)
        if outcome.failure:
            return [], outcome.usage, outcome.failure

        picked: list[tuple[int, int]] = []
        for (ask_index, chunk_no, _options), decision in zip(group, outcome.decisions, strict=True):
            if decision.index is not None:
                picked.append((ask_index, chunk_no * size + decision.index))
        return picked, outcome.usage, ""

    async def _run(self, questions: Sequence[ports.Question]) -> RerankOutcome:
        try:
            result = await self._model.choose(questions)
        except Exception as exc:  # noqa: BLE001 - 外部モデルの不調で落とさない
            # API サーバとして 500 を返さないために、ここで握って呼び出し側に
            # 候補の先頭へフォールバックさせる。アダプタが包み忘れた
            # 想定外の例外も同じ扱いにする。
            _log.warning("判定モデルの呼び出しに失敗: %s", exc)
            return RerankOutcome(decisions=[], usage=Usage(), failure=str(exc))
        decisions = list(result.decisions)
        # 契約では問と同じ長さだが、足りなければ「答えなし」で埋める。
        decisions.extend(Decision.unanswered() for _ in range(len(questions) - len(decisions)))
        return RerankOutcome(decisions=decisions, usage=result.usage)


#: 1 選択肢あたりの固定費（JSON の鍵と区切り）と、日本語 1 文字あたりの
#: トークン数。実測（254 件・4,211 文字で 9,727 トークン）から取った。
_TOKENS_PER_CHAR = 2.0
_TOKENS_PER_OPTION = 6
#: 質問文と材料の分。
_REQUEST_OVERHEAD = 800


def _estimate_tokens(options: Sequence[str]) -> int:
    chars = sum(len(option) for option in options)
    return int(chars * _TOKENS_PER_CHAR) + len(options) * _TOKENS_PER_OPTION


def _pack(
    jobs: Sequence[tuple[int, int, Sequence[str]]], budget: int, max_asks: int
) -> list[list[tuple[int, int, Sequence[str]]]]:
    """分割をリクエストにまとめる。

    トークン量が ``budget`` に収まり、1 リクエストに載る入力が ``max_asks``
    を超えないようにする。後者はトークンではなく精度のための制約で、
    複数の入力を詰めると絞り込みが鈍ることが実測で出ている。
    """
    groups: list[list[tuple[int, int, Sequence[str]]]] = []
    current: list[tuple[int, int, Sequence[str]]] = []
    used = _REQUEST_OVERHEAD
    asks: set[int] = set()
    for job in jobs:
        cost = _estimate_tokens(job[2])
        too_many_asks = job[0] not in asks and len(asks) >= max(1, max_asks)
        if current and (used + cost > budget or too_many_asks):
            groups.append(current)
            current = []
            used = _REQUEST_OVERHEAD
            asks = set()
        current.append(job)
        asks.add(job[0])
        used += cost
    if current:
        groups.append(current)
    return groups
