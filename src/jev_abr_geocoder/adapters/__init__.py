"""外部依存の実装。**具体的なライブラリの名前が出るのはこのパッケージだけ。**

======================================  ===================  ==============
ポート                                    実装                   ライブラリ
======================================  ===================  ==============
``IndexReader`` / ``IndexWriter``        ``SqliteStore``      ``sqlite3``
``PrefixTrie`` / ``TrieFactory``         ``MarisaTries``      ``marisa-trie``
``DecisionModel``                       ``JevModel``         ``typesafe-sdk``
``HttpClient``                          ``HttpxClient``      ``httpx``
======================================  ===================  ==============

ここにある関数が**既定の組み合わせ**で、差し替えたい呼び出し側は同じポートを
満たす別の実装を渡せばよい。コア側（``index/`` / ``match/`` / ``geocoder.py``）
はこのパッケージを import しない。
"""

from __future__ import annotations

from pathlib import Path

from .. import ports
from ..config import GeocoderConfig
from ..index.townindex import TownIndex
from .http import HttpxClient
from .jev import JevModel
from .marisa import TRIE_FILENAME, MarisaTries
from .sqlite import DB_FILENAME, SCHEMA_VERSION, SqliteStore

__all__ = [
    "DB_FILENAME",
    "SCHEMA_VERSION",
    "TRIE_FILENAME",
    "HttpxClient",
    "JevModel",
    "MarisaTries",
    "SqliteStore",
    "http_client",
    "tries",
    "index_path",
    "trie_path",
    "open_reader",
    "create_writer",
    "open_index",
    "jev_model",
]


def http_client() -> ports.HttpClient:
    return HttpxClient()


def tries() -> ports.TrieFactory:
    return MarisaTries()


def index_path(data_dir: Path) -> Path:
    """索引本体のパス。存在確認に使う。"""
    return data_dir / DB_FILENAME


def trie_path(data_dir: Path) -> Path:
    """前方一致トライのパス。構築の書き込み先。"""
    return data_dir / TRIE_FILENAME


def open_reader(data_dir: Path) -> ports.IndexReader:
    return SqliteStore.open(data_dir / DB_FILENAME, readonly=True)


def create_writer(data_dir: Path) -> ports.IndexWriter:
    return SqliteStore.create(data_dir / DB_FILENAME)


def open_index(data_dir: Path) -> TownIndex:
    """層1 を開く。トライを mmap し、レコードを読み取り専用で開く。"""
    factory = MarisaTries()
    return TownIndex(
        trie=factory.load(data_dir / TRIE_FILENAME),
        store=open_reader(data_dir),
        tries=factory,
    )


def jev_model(cfg: GeocoderConfig) -> ports.DecisionModel:
    return JevModel.from_env(cfg)
