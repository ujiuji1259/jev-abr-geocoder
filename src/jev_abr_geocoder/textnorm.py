"""入力文字列の正規化。

**ここには表記ゆれを吸収する規則を書かない。** NFKC 正規化と空白の除去という、
判断を含まない機械的変換だけを行う。

「一丁目 と 1丁目 を同一視する」「大字を落とす」といった知識はすべて
:mod:`jev_abr_geocoder.index.keys` の索引側エイリアス生成に置く。そちらは
ロジックではなくデータなので壊れない。詳細は docs/architecture.md を参照。
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["normalize"]

_WHITESPACE = re.compile(r"[\s　]+")


def normalize(text: str) -> str:
    """NFKC 正規化して空白を除去する。冪等。"""
    return _WHITESPACE.sub("", unicodedata.normalize("NFKC", text))
