"""Jev アダプタ。

Choice の組み立てと応答の解釈は、コアから見えない壁の内側にある。ここが
**唯一その形を検証する場所**なので、API の制約（255 件）と確率分布の読み方を
固定しておく。ネットワークには出ず、偽のクライアントで組み立てだけを見る。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from jev_abr_geocoder import ports
from jev_abr_geocoder.adapters.jev import MAX_CHOICE_OPTIONS, JevModel
from jev_abr_geocoder.config import NONE_OPTION


class _FakeClient:
    """``system_one`` の呼び出しを記録し、決めた答えを返す。"""

    def __init__(self, answers: Mapping[str, Any] | None = None, error: Exception | None = None):
        self.calls: list[tuple[Any, Mapping[str, Any]]] = []
        self._answers = answers or {}
        self._error = error

    async def system_one(self, *, state: Any, questions: Mapping[str, Any], **_: Any) -> Any:
        self.calls.append((state, questions))
        if self._error is not None:
            raise self._error
        return _FakeResponse(self._answers)


class _FakeResponse:
    def __init__(self, answers: Mapping[str, Any]) -> None:
        self.answers = answers
        self.usage = _FakeUsage()


class _FakeUsage:
    input_tokens = 1234
    output_tokens = 56


class _FakeAnswer:
    def __init__(self, choice: str, probabilities: Mapping[str, float], confidence: float) -> None:
        self.choice = choice
        self.probabilities = probabilities
        self.confidence = confidence


def _question(options: Sequence[str], **kwargs: Any) -> ports.Question:
    return ports.Question(
        subject=kwargs.pop("subject", {"入力": "鳥取市面影1-2"}),
        question=kwargs.pop("question", "どれか"),
        options=options,
        label=kwargs.pop("label", "住所"),
        **kwargs,
    )


async def test_choice_never_exceeds_the_api_limit() -> None:
    """選択肢は「該当なし」を含めて 255 件を超えてはならない。

    超えると API が 400 を返し、**バッチ全体が失敗する**。候補を 255 件に
    切ってから「該当なし」を足して 256 になる off-by-one を実際に踏んだ。
    """
    client = _FakeClient()
    model = JevModel(client, "jev-latest", 30.0)
    await model.choose([_question([f"候補{i}" for i in range(400)])])

    _state, questions = client.calls[0]
    criteria = next(iter(questions.values())).criteria
    assert len(criteria) == MAX_CHOICE_OPTIONS
    assert NONE_OPTION in criteria


async def test_same_subject_is_stated_once() -> None:
    """同じ材料を問ごとに複製しない。

    分割絞り込み は 1 つの入力について何十問も並べるので、材料を
    複製するとトークンがそのぶん嵩む。
    """
    subject = {"入力": "福井市中央1-1"}
    client = _FakeClient()
    model = JevModel(client, "jev-latest", 30.0)
    await model.choose([_question(["a"], subject=subject), _question(["b"], subject=subject)])

    state, questions = client.calls[0]
    assert len(state) == 1
    assert len(questions) == 2
    # 質問は材料を state の鍵で参照する。
    key = next(iter(state))
    for question in questions.values():
        assert question.instructions["対象"] == f"`{key}`"


async def test_cited_labels_are_referenced_by_name() -> None:
    """質問が材料の一部を名前で指す場合（「`町字` より後ろ」）。"""
    client = _FakeClient()
    model = JevModel(client, "jev-latest", 30.0)
    await model.choose(
        [_question(["1-2"], subject={"入力": "x", "町字": "面影一丁目"}, refers_to=("町字",))]
    )

    state, questions = client.calls[0]
    key = next(iter(state))
    assert next(iter(questions.values())).instructions["町字"] == f"`{key}.町字`"


async def test_answer_is_mapped_to_the_option_index() -> None:
    client = _FakeClient(
        answers={"a0": _FakeAnswer("c1", {"c0": 0.1, "c1": 0.8, NONE_OPTION: 0.1}, 0.8)}
    )
    model = JevModel(client, "jev-latest", 30.0)
    result = await model.choose([_question(["面影一丁目", "面影二丁目"])])

    decision = result.decisions[0]
    assert decision.index == 1
    assert decision.probability == pytest.approx(0.8)
    assert decision.contains_answer == pytest.approx(0.9)
    assert result.usage.requests == 1
    assert result.usage.input_tokens == 1234


async def test_none_option_means_no_index() -> None:
    """「該当なし」は index=None と低い contains_answer で返る。"""
    client = _FakeClient(
        answers={"a0": _FakeAnswer(NONE_OPTION, {"c0": 0.1, NONE_OPTION: 0.9}, 0.9)}
    )
    model = JevModel(client, "jev-latest", 30.0)
    result = await model.choose([_question(["面影一丁目"])])

    assert result.decisions[0].index is None
    assert result.decisions[0].contains_answer == pytest.approx(0.1)


async def test_missing_answer_is_unanswered() -> None:
    """答えが返ってこなかった問も、問と同じ長さで埋めて返す。"""
    client = _FakeClient(answers={})
    model = JevModel(client, "jev-latest", 30.0)
    result = await model.choose([_question(["a"]), _question(["b"])])

    assert len(result.decisions) == 2
    assert all(d.index is None for d in result.decisions)


async def test_vendor_failure_becomes_model_unavailable() -> None:
    """ベンダの例外はポートの語彙に直す。コアはこれだけを知っていればよい。"""
    client = _FakeClient(error=RuntimeError("429 rate limited"))
    model = JevModel(client, "jev-latest", 30.0)
    with pytest.raises(ports.ModelUnavailable, match="429"):
        await model.choose([_question(["a"])])


async def test_no_questions_costs_nothing() -> None:
    client = _FakeClient()
    model = JevModel(client, "jev-latest", 30.0)
    result = await model.choose([])
    assert result.decisions == []
    assert result.usage.requests == 0
    assert not client.calls
