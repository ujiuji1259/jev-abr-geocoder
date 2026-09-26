"""判定モデルの答えと、その使用量。

:mod:`ports` の :class:`DecisionModel` が返す値型。**閾値との比較はここでは
しない** — 採否は ``assemble`` が ``config`` の閾値を見て決める。

どちらも frozen。使用量は段ごとに出るので、``+`` で畳めるようにしてある。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Decision", "Usage"]


@dataclass(frozen=True, slots=True)
class Decision:
    """判定モデルの結果。閾値との比較はここではなく geocoder が行う。"""

    index: int | None
    probability: float
    confidence: float
    contains_answer: float
    #: モデルを呼ばずに決めた場合 True
    fast_path: bool = False

    @classmethod
    def fast(cls) -> Decision:
        """選ぶ余地が無いので訊かずに決めた。確信度は満点で通す。"""
        return cls(index=0, probability=1.0, confidence=1.0, contains_answer=1.0, fast_path=True)

    @classmethod
    def unverified(cls) -> Decision:
        """モデルに訊けなかったので候補の先頭を採る。

        確信度 0 なので、``geocoder`` は粒度を 1 段上げて返す。
        """
        return cls(index=0, probability=0.0, confidence=0.0, contains_answer=0.0)

    @classmethod
    def unanswered(cls) -> Decision:
        """答えが得られなかった。何も採用しない。"""
        return cls(index=None, probability=0.0, confidence=0.0, contains_answer=0.0)


@dataclass(frozen=True, slots=True)
class Usage:
    """判定モデルの使用量。**足し算で畳む。**

    >>> Usage(100, 10, 1) + Usage(50, 5, 1)
    Usage(input_tokens=150, output_tokens=15, requests=2)
    """

    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            requests=self.requests + other.requests,
        )
