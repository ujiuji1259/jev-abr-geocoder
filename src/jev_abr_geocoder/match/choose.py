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
from dataclasses import dataclass, replace

from .. import ports
from ..config import (
    ADDRESS_LABEL,
    BANCHI_LABEL,
    BANCHI_QUESTION,
    MACHIAZA_QUESTION,
    OAZA_QUESTION,
    GeocoderConfig,
)
from ..decision import Decision, Usage

__all__ = [
    "Chooser",
    "NarrowOutcome",
    "ChoiceOutcome",
    "machiaza_question",
    "banchi_question",
    "oaza_question",
]

_log = logging.getLogger(__name__)


# --------------------------------------------------------------- 問いの組み立て


def machiaza_question(query: str, normalized: str, options: Sequence[str]) -> ports.Question:
    """候補のどれが入力の町字かを訊く。"""
    return ports.Question(
        subject={"入力": query, "正規化": normalized},
        question=MACHIAZA_QUESTION,
        options=options,
        label=ADDRESS_LABEL,
    )


def banchi_question(query: str, town: str, tail: str, options: Sequence[str]) -> ports.Question:
    """町字が決まったあと、残りの数値列がどの番号かを訊く。"""
    return ports.Question(
        subject={"入力": query, "町字": town, "町字より後ろ": tail},
        question=BANCHI_QUESTION,
        options=options,
        label=BANCHI_LABEL,
        refers_to=("町字",),
    )


def oaza_question(query: str, normalized: str, options: Sequence[str]) -> ports.Question:
    """候補が上限を超えたとき、先に大字だけを訊く。

    ここに渡す選択肢は Choice の上限を超えていてよい。:meth:`Chooser.narrow`
    が分割して投げる。
    """
    return ports.Question(
        subject={"入力": query, "正規化": normalized},
        question=OAZA_QUESTION,
        options=options,
        label=ADDRESS_LABEL,
    )


# ----------------------------------------------------------------- 答えの型


@dataclass(slots=True)
class ChoiceOutcome:
    #: 渡した問と同じ順・同じ長さ。訊けなかったときだけ空。
    decisions: list[Decision]
    usage: Usage
    #: モデルを呼べなかった場合の理由。空なら正常。
    failure: str = ""


@dataclass(slots=True)
class NarrowOutcome:
    """各問について、勝ち残った選択肢の添字。"""

    survivors: list[list[int]]
    usage: Usage
    failure: str = ""


class Chooser:
    def __init__(self, model: ports.DecisionModel, cfg: GeocoderConfig) -> None:
        self._model = model
        self._cfg = cfg

    async def ask(self, questions: Sequence[ports.Question]) -> ChoiceOutcome:
        """**1 リクエストで**全問に答えさせる。例外は投げない。"""
        if not questions:
            return ChoiceOutcome(decisions=[], usage=Usage())
        try:
            result = await self._model.choose(questions)
        except Exception as exc:  # noqa: BLE001 - 外部モデルの不調で落とさない
            # API サーバとして 500 を返さないために、ここで握って呼び出し側に
            # 候補の先頭へフォールバックさせる。アダプタが包み忘れた想定外の
            # 例外も同じ扱いにする。
            _log.warning("判定モデルの呼び出しに失敗: %s", exc)
            return ChoiceOutcome(decisions=[], usage=Usage(), failure=str(exc))
        decisions = list(result.decisions)
        # 契約では問と同じ長さだが、足りなければ「答えなし」で埋める。
        decisions.extend(Decision.unanswered() for _ in range(len(questions) - len(decisions)))
        return ChoiceOutcome(decisions=decisions, usage=result.usage)

    async def narrow(self, questions: Sequence[ports.Question]) -> NarrowOutcome:
        """選択肢が Choice の上限を超えた問を、分割して絞る。

        一覧を上限ごとに分割し、各分割について「この一覧の中にあるか」を訊く。
        判定モデルは 1 リクエスト内の全問を並列に評価するので、分割を詰め込める
        だけ往復は減る。

        ただし **上限は 64k tokens/リクエスト**で、実測では 254 件の選択肢
        1 分割が 9,727 トークン。超えるとリクエストごと失敗するので、トークン量を
        見積もって ``narrow_token_budget`` に収まるように詰め、**並行に**投げる。

        絞り込みの基準はここでも設けない。どれを残すかはモデルが決める。
        """
        if not questions:
            return NarrowOutcome(survivors=[], usage=Usage())

        size = self._cfg.max_candidates
        # (問の添字, 分割番号, 選択肢) を平らに並べてから詰める。
        chunks = [
            _Chunk(index, number, question.options[start : start + size])
            for index, question in enumerate(questions)
            for number, start in enumerate(range(0, len(question.options), size))
        ]
        groups = _pack(chunks, self._cfg.narrow_token_budget, self._cfg.narrow_inputs_per_request)
        results = await asyncio.gather(*(self._narrow_group(questions, g) for g in groups))

        survivors: list[list[int]] = [[] for _ in questions]
        usage = Usage()
        failure = ""
        for picked, group_usage, group_failure in results:
            usage.merge(group_usage)
            failure = failure or group_failure
            for question_index, option_index in picked:
                survivors[question_index].append(option_index)
        for kept in survivors:
            kept.sort()
        return NarrowOutcome(survivors=survivors, usage=usage, failure=failure)

    async def _narrow_group(
        self, questions: Sequence[ports.Question], group: Sequence[_Chunk]
    ) -> tuple[list[tuple[int, int]], Usage, str]:
        size = self._cfg.max_candidates
        outcome = await self.ask(
            [replace(questions[chunk.question], options=chunk.options) for chunk in group]
        )
        if outcome.failure:
            return [], outcome.usage, outcome.failure

        picked: list[tuple[int, int]] = []
        for chunk, decision in zip(group, outcome.decisions, strict=True):
            if decision.index is not None:
                picked.append((chunk.question, chunk.number * size + decision.index))
        return picked, outcome.usage, ""


@dataclass(frozen=True, slots=True)
class _Chunk:
    """1 つの問の選択肢を上限ごとに切った 1 片。"""

    #: 元の問の添字
    question: int
    #: その問の中での分割番号。勝った添字を元の位置に戻すのに使う。
    number: int
    options: Sequence[str]


#: 1 選択肢あたりの固定費（JSON の鍵と区切り）と、日本語 1 文字あたりの
#: トークン数。実測（254 件・4,211 文字で 9,727 トークン）から取った。
_TOKENS_PER_CHAR = 2.0
_TOKENS_PER_OPTION = 6
#: 質問文と材料の分。
_REQUEST_OVERHEAD = 800


def _estimate_tokens(options: Sequence[str]) -> int:
    chars = sum(len(option) for option in options)
    return int(chars * _TOKENS_PER_CHAR) + len(options) * _TOKENS_PER_OPTION


def _pack(chunks: Sequence[_Chunk], budget: int, max_asks: int) -> list[list[_Chunk]]:
    """分割をリクエストにまとめる。

    トークン量が ``budget`` に収まり、1 リクエストに載る入力が ``max_asks``
    を超えないようにする。後者はトークンではなく精度のための制約で、
    複数の入力を詰めると絞り込みが鈍ることが実測で出ている。
    """
    groups: list[list[_Chunk]] = []
    current: list[_Chunk] = []
    used = _REQUEST_OVERHEAD
    seen: set[int] = set()
    for chunk in chunks:
        cost = _estimate_tokens(chunk.options)
        too_many = chunk.question not in seen and len(seen) >= max(1, max_asks)
        if current and (used + cost > budget or too_many):
            groups.append(current)
            current = []
            used = _REQUEST_OVERHEAD
            seen = set()
        current.append(chunk)
        seen.add(chunk.question)
        used += cost
    if current:
        groups.append(current)
    return groups
