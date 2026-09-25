"""町字より後ろの数値列の取り出し。

**ここで「どこまでが住所か」を決めない。** 「1-2-3 〇〇ハイツ301」の 301 が
番地なのか部屋番号なのかは、その町字に実在する番号を見ないと決まらない。
規則で書くと必ず破綻するので、判断は Jev に渡す（docs/architecture.md）。

このモジュールの仕事は、層2 の BLOB を絞り込むための **先頭の数値** を拾うことと、
Jev に見せる残り文字列をそのまま保つことだけ。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Tail", "parse_tail"]

#: 数値の前後に現れる区切り。NFKC 後なので全角は考えなくてよいが、
#: 長音符・各種ダッシュ・中黒は NFKC でも残る。
_SEPARATORS = "-ー−–—―‐‑‒〜~/・.,、 　"

_LEADING_JUNK = re.compile(rf"^[{re.escape(_SEPARATORS)}]+")
_NUMBER = re.compile(r"\d+")

#: 数値の直後に来ると「まだ住所が続く」ことを示す助数詞。
_CONTINUES = ("丁目", "丁", "番地", "番", "号", "地割", "の", "ノ")


@dataclass(frozen=True, slots=True)
class Tail:
    """町字より後ろの部分。"""

    #: 先頭から素直に読める数値列。層2 の絞り込みと突き合わせに使う。
    numbers: tuple[int, ...]
    #: 入力の残りそのもの。建物名を含んだまま Jev に渡す。
    raw: str

    @property
    def first(self) -> int | None:
        return self.numbers[0] if self.numbers else None

    def __bool__(self) -> bool:
        return bool(self.raw)


def parse_tail(text: str) -> Tail:
    """残り文字列から先頭の数値列を拾う。

    数値と数値の間に助数詞か区切り記号しか無い限り読み進め、それ以外の文字
    （建物名の始まり）が現れたら止める。止めたあとの文字列も ``raw`` には
    残っているので、Jev は全体を見て判断できる。

    >>> parse_tail("1-2-3〇〇ハイツ301").numbers
    (1, 2, 3)
    >>> parse_tail("1丁目2番3号").numbers
    (1, 2, 3)
    """
    raw = text
    work = _LEADING_JUNK.sub("", text)
    numbers: list[int] = []
    pos = 0
    while True:
        match = _NUMBER.match(work, pos)
        if match is None:
            break
        numbers.append(int(match.group(0)))
        pos = match.end()
        gap_end = _next_number_start(work, pos)
        if gap_end is None:
            break
        pos = gap_end
    return Tail(numbers=tuple(numbers[:3]), raw=raw)


def _next_number_start(work: str, pos: int) -> int | None:
    """``pos`` から次の数値までの間が、住所の区切りとして許せるかを見る。

    許せるなら次の数値の開始位置、許せないなら None。
    """
    match = _NUMBER.search(work, pos)
    if match is None:
        return None
    gap = work[pos : match.start()]
    if not gap:
        return match.start()
    stripped = gap.strip(_SEPARATORS)
    if not stripped:
        return match.start()
    if stripped in _CONTINUES:
        return match.start()
    # 「番地の」のように助数詞が連なる場合も住所の続きとみなす。
    remainder = stripped
    while remainder:
        for token in _CONTINUES:
            if remainder.startswith(token):
                remainder = remainder[len(token) :].strip(_SEPARATORS)
                break
        else:
            return None
    return match.start()
