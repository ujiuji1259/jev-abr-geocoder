"""層1 の住所索引。

前方一致トライと町字レコードを 1 つにまとめ、「入力の先頭に一致する町字」を
引けるようにする。**トライの実装も永続化の実装も知らない**（:mod:`ports` の
:class:`PrefixTrie` と :class:`IndexReader` だけを見る）。実体は
``adapters/marisa.py`` と ``adapters/sqlite.py`` で、開くのは
:func:`adapters.open_index`。

市区町村索引 (約 8k 鍵) は小さいので、レコードから開くたびにメモリ上で
組み立てる。配布ファイルを増やさずに済む。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .. import ports
from ..address import CityName, CityRecord, PrefRecord
from ..textnorm import normalize
from .keys import city_aliases, pref_variants

__all__ = ["MachiazaIndex", "MachiazaMatch", "CityMatch"]


@dataclass(frozen=True, slots=True)
class MachiazaMatch:
    row_id: int
    #: 一致した索引鍵（正規化済み）
    key: str

    @property
    def matched_len(self) -> int:
        return len(self.key)


@dataclass(frozen=True, slots=True)
class CityMatch:
    lg_code: int
    key: str

    @property
    def matched_len(self) -> int:
        return len(self.key)


class MachiazaIndex:
    """層1 の読み取り。プロセス間で安全に共有できる。"""

    def __init__(
        self,
        trie: ports.PrefixTrie,
        reader: ports.IndexReader,
        *,
        tries: ports.TrieBackend,
    ) -> None:
        self._trie = trie
        self._reader = reader
        self._prefs: list[PrefRecord] = reader.prefs()
        self._cities: list[CityRecord] = reader.cities()
        self._city_by_lg = {c.lg_code: c for c in self._cities}
        # 全国地方公共団体コードの上位 2 桁が都道府県コード。
        # 都道府県自身は 010006 のようにチェックディジットが付くので、上位で引く。
        self._pref_by_code = {p.lg_code // 10_000: p for p in self._prefs}
        self._city_trie = tries.build(_city_alias_pairs(self._cities))
        self._pref_keys = _build_pref_keys(self._prefs)

    def close(self) -> None:
        self._reader.close()

    @property
    def reader(self) -> ports.IndexReader:
        """町字レコードと層2 の番号を引く先。"""
        return self._reader

    def city(self, lg_code: int) -> CityRecord | None:
        """全国地方公共団体コードから市区町村を引く。"""
        return self._city_by_lg.get(lg_code)

    def pref(self, lg_code: int) -> PrefRecord | None:
        """任意の全国地方公共団体コードから都道府県を引く。"""
        return self._pref_by_code.get(lg_code // 10_000)

    # --------------------------------------------------------- 前方一致

    def prefixes(self, text: str) -> list[MachiazaMatch]:
        """``text`` の前方一致となる町字鍵をすべて返す。長い順。"""
        hits = [
            MachiazaMatch(row_id=row_id, key=key) for key, row_id in self._trie.prefixes_of(text)
        ]
        hits.sort(key=lambda h: -h.matched_len)
        return hits

    def city_prefixes(self, text: str) -> list[CityMatch]:
        """``text`` の前方一致となる市区町村鍵を返す。長い順。"""
        hits = [
            CityMatch(lg_code=lg_code, key=key)
            for key, lg_code in self._city_trie.prefixes_of(text)
        ]
        hits.sort(key=lambda h: -h.matched_len)
        return hits

    def pref_prefix(self, text: str) -> tuple[PrefRecord, str] | None:
        """``text`` の先頭に一致する都道府県。47 件なので線形で足りる。"""
        best: tuple[PrefRecord, str] | None = None
        for key, pref in self._pref_keys:
            if text.startswith(key) and (best is None or len(key) > len(best[1])):
                best = (pref, key)
        return best

    def machiaza_extensions(self, prefix: str, limit: int) -> list[tuple[str, int]]:
        """``prefix`` で始まる索引鍵と、その町字。

        入力が索引鍵の先頭になっているケース（丁目や小字の省略）を拾うための
        完全一致の探索で、曖昧一致ではない。
        """
        return self._trie.extensions_of(prefix, limit)


def _city_alias_pairs(cities: Sequence[CityRecord]) -> list[tuple[str, int]]:
    return [
        (key, record.lg_code)
        for record in cities
        for key in city_aliases(CityName(record.pref, record.county, record.city, record.ward))
    ]


def _build_pref_keys(prefs: Sequence[PrefRecord]) -> list[tuple[str, PrefRecord]]:
    out: list[tuple[str, PrefRecord]] = []
    for record in prefs:
        for variant in pref_variants(record.pref):
            if variant:
                out.append((normalize(variant), record))
    return out
