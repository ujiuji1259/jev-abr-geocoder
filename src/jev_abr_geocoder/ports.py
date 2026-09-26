"""外部世界との境界。

**このパッケージが外に要求することを、ここだけで宣言する。**

``sqlite3`` / ``marisa-trie`` / ``httpx`` / ``typesafe-sdk`` を import して
いいのは :mod:`jev_abr_geocoder.adapters` の中だけで、コア（``index/`` /
``match/`` / ``geocoder.py``）はこのファイルの Protocol しか知らない。
どの実装を使うかを決めるのは合成の根（``adapters/__init__.py``、
:meth:`Geocoder.open`、``cli.py``、:func:`index.build.build`）だけ。

そのために、**ポートの引数と戻り値にベンダ固有の型を出さない**。境界で要る
値型（:class:`HttpResponse` / :class:`Question`）もここに置く。外部ライブラリ
の型が 1 つでも漏れると、その型を触るすべてのコードがそのライブラリに縛られる。

効くのはテストで、``tests/conftest.py`` の ``FakeModel`` は
:class:`DecisionModel` を満たすだけの 20 行で済む。Jev の Choice の作り方も
応答の形も知らなくてよい（docs/code-design.md 制約4）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .address import Banchi, BanchiKind, CityRecord, MachiazaRecord, PrefRecord
from .decision import Decision, Usage

__all__ = [
    "HttpError",
    "HttpResponse",
    "HttpClient",
    "PrefixTrie",
    "TrieBackend",
    "BanchiSource",
    "IndexReader",
    "IndexWriter",
    "Question",
    "Answers",
    "DecisionModel",
    "ModelUnavailable",
]


# ------------------------------------------------------------------ HTTP


class HttpError(RuntimeError):
    """応答が 4xx / 5xx だった。"""

    def __init__(self, status_code: int, url: str) -> None:
        super().__init__(f"HTTP {status_code}: {url}")
        self.status_code = status_code
        self.url = url


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    content: bytes
    headers: Mapping[str, str] = field(default_factory=dict)
    url: str = ""

    @property
    def not_modified(self) -> bool:
        """304。条件付き GET でキャッシュがそのまま使える。"""
        return self.status_code == 304

    def header(self, name: str) -> str | None:
        """ヘッダを大小文字を無視して引く。"""
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HttpError(self.status_code, self.url)


class HttpClient(Protocol):
    """GET だけ。ABR も Geolonia も取得しかしない。"""

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        """4xx / 5xx でも例外にせず返す。304 を見たい呼び出し側があるため。"""
        ...

    async def close(self) -> None: ...


# ------------------------------------------------------------------ トライ


class PrefixTrie(Protocol):
    """前方一致だけを提供する読み取り専用の索引。

    鍵は正規化済みのエイリアス、値は町字・市区町村への整数 ID。
    """

    def prefixes_of(self, text: str) -> list[tuple[str, int]]:
        """``text`` の先頭に一致する (鍵, 値) をすべて返す。順序は問わない。

        1 つの鍵に複数の値が付いていることがある（同名の町字）ので、
        鍵ごとに 1 組とは限らない。
        """
        ...

    def extensions_of(self, prefix: str, limit: int) -> list[tuple[str, int]]:
        """``prefix`` で始まる鍵を高々 ``limit`` 組返す。"""
        ...


class TrieBackend(Protocol):
    """トライの作成と読み込み。永続化の形式はアダプタが決める。"""

    def load(self, path: Path) -> PrefixTrie:
        """永続化されたトライを開く。"""
        ...

    def build(self, pairs: Iterable[tuple[str, int]]) -> PrefixTrie:
        """メモリ上に組む。市区町村トライ（約 8k 鍵）用。"""
        ...

    def save(self, pairs: Iterable[tuple[str, int]], path: Path) -> None:
        """組んで ``path`` に置く。

        **原子的に差し替える。** サーバが読んでいる最中に構築しても壊れない
        ことが要件（docs/code-design.md §8）。
        """
        ...


# ------------------------------------------------------------------ 永続化


class BanchiSource(Protocol):
    """層2 の番号だけを引く口。

    :class:`IndexReader` はこれを満たす。:mod:`match.numbers` はこちらしか
    要らないので、**要るものだけを要求する**ためにこの幅で切ってある。
    テストの偽物も 1 メソッドで済む。
    """

    def fetch_banchi(
        self,
        lg_code: int,
        machiaza_id: int,
        kind: BanchiKind,
        *,
        num1: int | None = None,
    ) -> list[Banchi]:
        """町字配下の番号を返す。

        ``num1`` を与えたら、その番号を含む範囲だけを展開してよい（町字の
        大きさによらず一定時間で返すための逃げ道）。収録が無ければ空。
        """
        ...


class IndexReader(BanchiSource, Protocol):
    """索引の読み取り。プロセス間で安全に共有できること。

    層1 のレコードと層2 の番号を引く。**ここに出入りするのはすべて
    ``models.py`` の値型**で、行やカラムの形は漏らさない。
    """

    def prefs(self) -> list[PrefRecord]: ...

    def cities(self) -> list[CityRecord]: ...

    def machiaza(self, row_ids: Sequence[int]) -> dict[int, MachiazaRecord]:
        """町字を一括で引く。候補を絞ったあとにだけ呼ばれる。"""
        ...

    def meta(self) -> dict[str, str]:
        """索引のメタデータ。版・構築日時・帰属表示など。"""
        ...

    def machiaza_count(self) -> int: ...

    def banchi_count(self) -> int: ...

    def sources(self) -> dict[str, str | None]:
        """取り込み済みソース URL -> ``Last-Modified``。"""
        ...

    def close(self) -> None: ...


class IndexWriter(Protocol):
    """索引の構築。``index/build.py`` だけが使う。

    層1 は毎回作り直し、層2 はファイル単位で差分更新する。そのため
    ``replace_*`` は全置換、``put_numbers*`` は upsert。
    """

    def set_meta(self, key: str, value: str) -> None: ...

    def replace_prefs(self, records: Iterable[PrefRecord]) -> None: ...

    def replace_cities(self, records: Iterable[CityRecord]) -> None: ...

    def replace_machiaza(self, records: Iterable[MachiazaRecord]) -> None: ...

    def put_banchi(self, items: Iterable[tuple[int, int, BanchiKind, Sequence[Banchi]]]) -> int:
        """まとめて書く。戻り値は書いた町字数。"""
        ...

    def source_last_modified(self, url: str) -> str | None:
        """記録済みの ``Last-Modified``。未取り込みなら None。"""
        ...

    def mark_source(self, url: str, kind: str, last_modified: str | None, rows: int) -> None: ...

    def commit(self) -> None: ...

    def close(self) -> None: ...


# ------------------------------------------------------------- 判定モデル


class ModelUnavailable(RuntimeError):
    """判定モデルに訊けなかった。

    レート制限・タイムアウト・障害はすべてこれ。アダプタがベンダの例外を
    これに包み、コアは候補の先頭へ退避する。**API サーバとして、
    外部モデルの不調で 500 を返さない**（docs/code-design.md §6）。
    """


@dataclass(frozen=True, slots=True)
class Question:
    """「この選択肢のうちどれか」を訊く 1 問。

    :attr:`subject` が判断の材料、:attr:`question` がその材料を指して何を
    訊くかを述べる文。どちらも入力と ``config.py`` の文言だけから成り、
    ベンダ固有の構造を含まない。「候補のいずれでもない」はアダプタが足すので、
    :attr:`options` には実在の候補だけを入れる。
    """

    #: 判断の材料。ラベル -> 値。
    subject: Mapping[str, str]
    #: 質問文。``config.py`` 以外に置かない。
    question: str
    #: 選択肢。この並び順が :attr:`Decision.index` に対応する。
    options: Sequence[str]
    #: 選択肢が何であるかのラベル（「住所」「番号」）。これも ``config.py``。
    label: str
    #: 質問文が名前で参照する :attr:`subject` のラベル。
    #: 「`町字` より後ろ」のように質問が材料を指すときに要る。
    refers_to: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class Answers:
    #: 渡した問と同じ順・同じ長さ。答えが得られなかった問は ``index=None``。
    decisions: Sequence[Decision]
    usage: Usage


class DecisionModel(Protocol):
    """候補から 1 つ選ばせるモデル。

    Protocol にしてあるのは、テストで差し替えられるようにするため。これが
    無いと Jev 無しでは何も検証できなくなる（docs/code-design.md §6）。
    """

    async def choose(self, questions: Sequence[Question]) -> Answers:
        """**全問を 1 リクエストで**答える。

        1 問ずつ呼ぶ実装にしてはならない。入力が何件でも往復を高々 2 回に
        抑えるという設計全体が、ここが 1 回で済むことに乗っている
        （docs/code-design.md 制約1）。

        閾値との比較はしない。:class:`Decision` を返すだけで、採否は
        ``geocoder.py`` が ``config.py`` の閾値を見て決める。

        :raises ModelUnavailable: モデルに訊けなかったとき。
        """
        ...
