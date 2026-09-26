"""``marisa-trie`` を :class:`ports.PrefixTrie` に合わせる。

全国 727k 町字を、エイリアス込み 4.9M 鍵の marisa-trie (LOUDS 簡潔トライ) に
載せる。実測で **ディスク 24.6 MB、mmap ロード 0.2 ms、常駐 RSS 1 MB、
前方一致 1 回 5.3 µs**。素の dict トライは 1.3 GB で使い物にならない。

mmap されたファイルはページキャッシュ経由で全ワーカーが同一の物理メモリを
共有するので、API サーバでワーカーを何本立てても層1 のコストは増えない。

**marisa-trie を import していいのはこのファイルだけ。** 型スタブを持たない
ライブラリなので境界では ``Any`` になるが、:class:`ports.PrefixTrie` の
``list[tuple[str, int]]`` に直してから外へ出す。``Any`` をここで止めるのが
このモジュールの仕事。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import marisa_trie

from .. import ports

__all__ = ["MarisaTrie", "MarisaTries", "TRIE_FILENAME", "PAYLOAD_FORMAT"]

TRIE_FILENAME = "town.marisa"

#: ペイロードは町字レコードへのインデックス (town_id) だけ。
#: 鍵は 4.9M 本あるが町字は 727k 件なので、レコードを埋め込むと 6.8 倍冗長になる。
#: また鍵は NFKC 後のエイリアスであり、出力すべき ABR 正規表記とは別物。
PAYLOAD_FORMAT = "<I"


class MarisaTrie:
    """:class:`ports.PrefixTrie` の marisa 実装。"""

    def __init__(self, trie: Any) -> None:
        self._trie = trie

    def prefixes(self, text: str) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        for key in self._trie.prefixes(text):
            for payload in self._trie[key]:
                out.append((key, int(payload[0])))
        return out

    def under(self, prefix: str, limit: int) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        for key, payload in self._trie.items(prefix):
            out.append((key, int(payload[0])))
            if len(out) >= limit:
                break
        return out


class MarisaTries:
    """:class:`ports.TrieFactory` の marisa 実装。"""

    def load(self, path: Path) -> ports.PrefixTrie:
        trie = marisa_trie.RecordTrie(PAYLOAD_FORMAT)
        trie.mmap(str(path))
        return MarisaTrie(trie)

    def build(self, pairs: Iterable[tuple[str, int]]) -> ports.PrefixTrie:
        return MarisaTrie(self._raw(pairs))

    def save(self, pairs: Iterable[tuple[str, int]], path: Path) -> None:
        # サーバが読んでいる最中でも壊れないよう、一時ファイルに書いて差し替える。
        tmp = path.with_suffix(path.suffix + ".tmp")
        self._raw(pairs).save(str(tmp))
        tmp.replace(path)

    @staticmethod
    def _raw(pairs: Iterable[tuple[str, int]]) -> Any:
        return marisa_trie.RecordTrie(PAYLOAD_FORMAT, ((key, (value,)) for key, value in pairs))
