"""索引側のエイリアス鍵生成。

**表記ゆれに関する知識は、このファイルだけに置く。**

入力側の正規化 (:mod:`jev_abr_geocoder.textnorm`) は NFKC と空白除去しか
行わない。「一丁目 と 1丁目 は同じ」「大字 は省略され得る」といった知識は、
ABR が持つ列から機械的に鍵を増やすことで表現する。ロジックではなくデータなので、
漏れても黙って失敗せず、鍵を足せば直る。

「入力がこう来たらこう直す」と書きたくなったら、それは索引側の鍵を増やすべき
場面である。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..textnorm import normalize

__all__ = [
    "TownName",
    "CityName",
    "town_aliases",
    "city_aliases",
    "pref_variants",
    "kanji_number",
]

#: 都道府県名の接尾辞。「岩手花巻市」のように省略されることがある。
_PREF_SUFFIXES = ("都", "道", "府", "県")

#: 大字・字の接頭辞。ABR では 78,035 件の oaza_cho が「大字」で、
#: 254,638 件の koaza が「字」で始まるが、入力では落ちることが多い。
#: ABR は大字の列にも「字」を入れてくることがある（全国 21,315 件）。
#: 長い方から試す。
_OAZA_PREFIXES = ("大字", "字")
_KOAZA_PREFIXES = ("字",)


@dataclass(frozen=True, slots=True)
class CityName:
    pref: str
    county: str
    city: str
    ward: str


@dataclass(frozen=True, slots=True)
class TownName:
    pref: str
    county: str
    city: str
    ward: str
    oaza_cho: str
    chome: str
    chome_number: str
    koaza: str

    @property
    def city_name(self) -> CityName:
        return CityName(self.pref, self.county, self.city, self.ward)


def pref_variants(pref: str) -> tuple[str, ...]:
    """都道府県名の変種。空文字（完全省略）を含む。

    >>> pref_variants("岩手県")
    ('', '岩手県', '岩手')
    """
    if not pref:
        return ("",)
    out = ["", pref]
    for suffix in _PREF_SUFFIXES:
        if pref.endswith(suffix) and len(pref) > len(suffix):
            out.append(pref[: -len(suffix)])
            break
    return tuple(out)


def _strip_variants(value: str, prefixes: tuple[str, ...]) -> tuple[str, ...]:
    """接頭辞つきの語について、あり/なし両方を返す。"""
    if not value:
        return ("",)
    for prefix in prefixes:
        if value.startswith(prefix) and len(value) > len(prefix):
            return (value, value[len(prefix) :])
    return (value,)


_KANJI_DIGITS = "〇一二三四五六七八九"


def kanji_number(value: int) -> str:
    """整数を漢数字にする。丁目の番号なので 1..999 を想定。

    >>> kanji_number(1), kanji_number(12), kanji_number(20), kanji_number(105)
    ('一', '十二', '二十', '百五')
    """
    if value <= 0 or value >= 1000:
        return ""
    out = ""
    for unit_value, unit in ((100, "百"), (10, "十")):
        digit, value = divmod(value, unit_value)
        if digit:
            out += ("" if digit == 1 else _KANJI_DIGITS[digit]) + unit
    if value:
        out += _KANJI_DIGITS[value]
    return out


def _chome_variants(chome: str, chome_number: str) -> tuple[str, ...]:
    """丁目の変種。

    ABR の ``chome`` は表記が揺れており、「一丁目」のような漢数字のことも
    「１丁目」のような全角算用数字のこともある。どちらで収録されていても
    両方の表記で引けるように、``chome_number`` 列から漢数字・算用数字・
    丁目省略の 3 形を生成する。

    >>> _chome_variants("一丁目", "1")
    ('一丁目', '1丁目', '1')
    >>> _chome_variants("１丁目", "2")
    ('１丁目', '2丁目', '2', '二丁目')
    """
    if not chome:
        return ("",)
    out = [chome]
    if chome_number:
        kanji = kanji_number(int(chome_number)) if chome_number.isdigit() else ""
        forms = [f"{chome_number}丁目", chome_number]
        if kanji:
            forms.append(f"{kanji}丁目")
        for form in forms:
            if form not in out:
                out.append(form)
    return tuple(out)


def _city_cores(name: CityName) -> tuple[str, ...]:
    """郡の有無による市区町村部の変種。"""
    full = f"{name.county}{name.city}{name.ward}"
    if not name.county:
        return (full,)
    return (full, f"{name.city}{name.ward}")


def city_aliases(name: CityName) -> set[str]:
    """市区町村を指す鍵。層1 のフォールバック経路で使う。"""
    out: set[str] = set()
    for pref in pref_variants(name.pref):
        for core in _city_cores(name):
            key = normalize(pref + core)
            if key:
                out.add(key)
    return out


def town_aliases(name: TownName) -> set[str]:
    """町字を指す鍵をすべて返す。

    都道府県 (3) x 郡 (1-2) x 丁目 (1-3) の直積を、**大字・字の接頭辞の
    付け方 2 通り**（原文そのまま / 全部落とす）それぞれについて作る。

    接頭辞を直積の軸にしない。「大字は原文どおり書くが字は省く」のような
    混ぜ方は実在しないので、軸にすると引かれない鍵が増えるだけ。実測で
    大字・字の両方に接頭辞がある町字が 57,033 件 (7.7%) あり、そこが
    4 通りに膨らんでいた。
    """
    out: set[str] = set()
    chomes = _chome_variants(name.chome, name.chome_number)
    cores = _city_cores(name.city_name)
    prefs = pref_variants(name.pref)
    for oaza, koaza in _prefix_styles(name.oaza_cho, name.koaza):
        for pref in prefs:
            for core in cores:
                for chome in chomes:
                    key = normalize(pref + core + oaza + chome + koaza)
                    if key:
                        out.add(key)
    return out


def _prefix_styles(oaza: str, koaza: str) -> tuple[tuple[str, str], ...]:
    """大字・字の接頭辞の付け方。原文そのままと、どちらも落とした形。

    >>> _prefix_styles("大字福井", "字上町")
    (('大字福井', '字上町'), ('福井', '上町'))
    >>> _prefix_styles("福井", "")
    (('福井', ''),)
    """
    stripped = (
        _strip_variants(oaza, _OAZA_PREFIXES)[-1],
        _strip_variants(koaza, _KOAZA_PREFIXES)[-1],
    )
    if stripped == (oaza, koaza):
        return ((oaza, koaza),)
    return ((oaza, koaza), stripped)
