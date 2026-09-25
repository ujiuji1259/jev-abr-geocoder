"""層1 の住所トライ。

全国 727k 町字を、エイリアス込み 4.9M 鍵の marisa-trie (LOUDS 簡潔トライ) に
載せる。実測で **ディスク 24.6 MB、mmap ロード 0.2 ms、常駐 RSS 1 MB、
前方一致 1 回 5.3 µs**。素の dict トライは 1.3 GB で使い物にならない。

mmap されたファイルはページキャッシュ経由で全ワーカーが同一の物理メモリを
共有するので、API サーバでワーカーを何本立てても層1 のコストは増えない。

市区町村トライ (約 8k 鍵) は小さいので、``city`` テーブルから開くたびに
メモリ上で組み立てる。ファイルを増やさずに済む。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import marisa_trie

from ..models import CityRecord, PrefRecord
from ..textnorm import normalize
from .keys import CityName, city_aliases, pref_variants
from .store import DB_FILENAME, Store

__all__ = ["TownIndex", "TownHit", "CityHit", "TRIE_FILENAME", "PAYLOAD_FORMAT", "build_trie"]

#: marisa-trie は型スタブを持たないので、境界では Any として扱う。
#: この別名を使うのは「型が付かないのは marisa だけ」であることを示すため。
RecordTrie = Any

TRIE_FILENAME = "town.marisa"

#: ペイロードは町字レコードへのインデックス (town_id) だけ。
#: 鍵は 4.9M 本あるが町字は 727k 件なので、レコードを埋め込むと 6.8 倍冗長になる。
#: また鍵は NFKC 後のエイリアスであり、出力すべき ABR 正規表記とは別物。
PAYLOAD_FORMAT = "<I"


@dataclass(frozen=True, slots=True)
class TownHit:
    town_id: int
    #: 一致した索引鍵（正規化済み）
    key: str

    @property
    def matched_len(self) -> int:
        return len(self.key)


@dataclass(frozen=True, slots=True)
class CityHit:
    city_id: int
    key: str

    @property
    def matched_len(self) -> int:
        return len(self.key)


def build_trie(pairs: Iterable[tuple[str, tuple[int]]]) -> RecordTrie:
    """(鍵, (town_id,)) の列からトライを組む。"""
    return marisa_trie.RecordTrie(PAYLOAD_FORMAT, pairs)


class TownIndex:
    """層1 の読み取り。プロセス間で安全に共有できる。"""

    def __init__(self, trie: RecordTrie, store: Store) -> None:
        self._trie = trie
        self._store = store
        self._prefs: list[PrefRecord] = store.prefs()
        self._cities: list[CityRecord] = store.cities()
        self._city_by_id = {c.city_id: c for c in self._cities}
        self._city_by_lg = {c.lg_code: c for c in self._cities}
        # 全国地方公共団体コードの上位 2 桁が都道府県コード。
        # 都道府県自身は 010006 のようにチェックディジットが付くので、上位で引く。
        self._pref_by_code = {p.lg_code // 10_000: p for p in self._prefs}
        self._city_alias_pairs = _city_alias_pairs(self._cities)
        self._city_trie = marisa_trie.RecordTrie(
            PAYLOAD_FORMAT, ((key, (city_id,)) for key, city_id in self._city_alias_pairs)
        )
        self._pref_keys = _build_pref_keys(self._prefs)

    @classmethod
    def open(cls, data_dir: Path) -> TownIndex:
        """``town.marisa`` を mmap し、``abr.db`` を読み取り専用で開く。"""
        trie = marisa_trie.RecordTrie(PAYLOAD_FORMAT)
        trie.mmap(str(data_dir / TRIE_FILENAME))
        return cls(trie, Store.open(data_dir / DB_FILENAME, readonly=True))

    def close(self) -> None:
        self._store.close()

    @property
    def store(self) -> Store:
        return self._store

    @property
    def cities(self) -> Sequence[CityRecord]:
        return self._cities

    @property
    def prefs(self) -> Sequence[PrefRecord]:
        return self._prefs

    def city(self, city_id: int) -> CityRecord | None:
        return self._city_by_id.get(city_id)

    def city_by_lg(self, lg_code: int) -> CityRecord | None:
        return self._city_by_lg.get(lg_code)

    def pref_by_lg(self, lg_code: int) -> PrefRecord | None:
        """任意の全国地方公共団体コードから都道府県を引く。"""
        return self._pref_by_code.get(lg_code // 10_000)

    @property
    def city_alias_pairs(self) -> Sequence[tuple[str, int]]:
        """(市区町村エイリアス, city_id) の一覧。長い順。

        市区町村そのものが曖昧一致になったときの母集合。約 8k 件なので
        まるごと舐めても数ミリ秒で済む。
        """
        return self._city_alias_pairs

    # --------------------------------------------------------- 前方一致

    def prefixes(self, text: str) -> list[TownHit]:
        """``text`` の前方一致となる町字鍵をすべて返す。長い順。"""
        hits: list[TownHit] = []
        for key in self._trie.prefixes(text):
            for payload in self._trie[key]:
                hits.append(TownHit(town_id=int(payload[0]), key=key))
        hits.sort(key=lambda h: -h.matched_len)
        return hits

    def city_prefixes(self, text: str) -> list[CityHit]:
        """``text`` の前方一致となる市区町村鍵を返す。長い順。"""
        hits: list[CityHit] = []
        for key in self._city_trie.prefixes(text):
            for payload in self._city_trie[key]:
                hits.append(CityHit(city_id=int(payload[0]), key=key))
        hits.sort(key=lambda h: -h.matched_len)
        return hits

    def pref_prefix(self, text: str) -> tuple[PrefRecord, str] | None:
        """``text`` の先頭に一致する都道府県。47 件なので線形で足りる。"""
        best: tuple[PrefRecord, str] | None = None
        for key, pref in self._pref_keys:
            if text.startswith(key) and (best is None or len(key) > len(best[1])):
                best = (pref, key)
        return best

    def keys_under(self, prefix: str, limit: int) -> list[tuple[str, int]]:
        """``prefix`` で始まる索引鍵と、その町字。

        入力が索引鍵の先頭になっているケース（丁目や小字の省略）を拾うための
        完全一致の探索で、曖昧一致ではない。
        """
        out: list[tuple[str, int]] = []
        for key, payload in self._trie.items(prefix):
            out.append((key, int(payload[0])))
            if len(out) >= limit:
                break
        return out


def _city_alias_pairs(cities: Sequence[CityRecord]) -> list[tuple[str, int]]:
    pairs: list[tuple[str, int]] = []
    for record in cities:
        name = CityName(record.pref, record.county, record.city, record.ward)
        for key in city_aliases(name):
            pairs.append((key, record.city_id))
    pairs.sort(key=lambda item: -len(item[0]))
    return pairs


def _build_pref_keys(prefs: Sequence[PrefRecord]) -> list[tuple[str, PrefRecord]]:
    out: list[tuple[str, PrefRecord]] = []
    for record in prefs:
        for variant in pref_variants(record.pref):
            if variant:
                out.append((normalize(variant), record))
    return out
