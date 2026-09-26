"""SQLite で :class:`ports.IndexReader` と :class:`ports.IndexWriter` を実装する。

1 ファイルに層1 のレコード (``pref`` / ``city`` / ``town``) と層2 の番号 BLOB
(``num_blob``) が入る。配布物は ``town.marisa`` とこの ``abr.db`` の 2 つだけ。

読み取りは読み取り専用接続で開くので、複数ワーカーから安全に共有できる。

**sqlite3 を import していいのはこのファイルだけ。** 行やカラムの形は外に
出さず、境界では ``models.py`` の値型に直す。座標を 1e7 倍の整数で持つのも
ここだけの事情なので、:class:`Point` との変換もここで閉じる。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

from ..address import (
    Banchi,
    BanchiKind,
    CityRecord,
    MachiazaName,
    MachiazaRecord,
    Point,
    PrefRecord,
)
from ..index import banchi_codec

__all__ = ["SqliteStore", "SCHEMA_VERSION", "DB_FILENAME"]

SCHEMA_VERSION = 5
DB_FILENAME = "abr.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 取り込み済みソースファイル。再実行時の差分判定に使う。
CREATE TABLE IF NOT EXISTS source(
    url           TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    last_modified TEXT,
    rows          INTEGER NOT NULL DEFAULT 0,
    ingested_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pref(
    lg_code INTEGER PRIMARY KEY,
    pref    TEXT NOT NULL,
    lat_1e7 INTEGER,
    lon_1e7 INTEGER
);

CREATE TABLE IF NOT EXISTS city(
    -- 全国地方公共団体コード。市区町村トライのペイロードと一致する。
    lg_code INTEGER PRIMARY KEY,
    pref    TEXT NOT NULL,
    county  TEXT NOT NULL,
    city    TEXT NOT NULL,
    ward    TEXT NOT NULL,
    lat_1e7 INTEGER,
    lon_1e7 INTEGER
);

-- row_id は前方一致トライのペイロードと一致する。
CREATE TABLE IF NOT EXISTS machiaza(
    row_id       INTEGER PRIMARY KEY,
    lg_code       INTEGER NOT NULL,
    machiaza_id   INTEGER NOT NULL,
    pref          TEXT NOT NULL,
    county        TEXT NOT NULL,
    city          TEXT NOT NULL,
    ward          TEXT NOT NULL,
    oaza_cho      TEXT NOT NULL,
    chome         TEXT NOT NULL,
    koaza         TEXT NOT NULL,
    rsdt_addr_flg INTEGER NOT NULL,
    lat_1e7       INTEGER,
    lon_1e7       INTEGER,
    -- 'abr' か 'geolonia'。geolonia の行は ABR に無い町字を補うもので、
    -- machiaza_id を持たないため層2（街区・住居番号・地番）は引けない。
    source        TEXT NOT NULL DEFAULT 'abr',
    -- 同じ場所が別 machiaza_id でも収録されている場合の、残りの machiaza_id。
    -- カンマ区切り。ABR は同じ町字を「字青野」「青野」の 2 レコードに分けて
    -- 持つことがあり、地番が両方に分かれている。層2 はここも引く。
    alt_machiaza  TEXT NOT NULL DEFAULT ''
);
-- 同じ町字を 2 行に分けない。mt_town は住居表示と地番の両方を持つ町字を
-- rsdt_addr_flg 違いの 2 行で収録しているが、取り込み時に 1 行へまとめる。
-- 分かれていると表示が同一の選択肢を Jev に見せることになり、答えようがない。
CREATE UNIQUE INDEX IF NOT EXISTS machiaza_by_code ON machiaza(lg_code, machiaza_id, source);

-- 街区・住居番号・地番。町字単位でパックした BLOB（banchi_codec.py の形式）。
CREATE TABLE IF NOT EXISTS num_blob(
    lg_code     INTEGER NOT NULL,
    machiaza_id INTEGER NOT NULL,
    kind        INTEGER NOT NULL,
    n           INTEGER NOT NULL,
    data        BLOB NOT NULL,
    PRIMARY KEY(lg_code, machiaza_id, kind)
) WITHOUT ROWID;
"""


class SqliteStore:
    """``abr.db`` への読み書き。

    読み取りポートと書き込みポートを 1 クラスで実装する。同じスキーマの表裏
    でしかないので分けても得が無いが、**呼び出し側はどちらかのポートとして
    しか受け取らない**（``geocoder`` は読み取り、``build`` は書き込み）。

    読み取り専用で開いた場合、書き込み系メソッドは sqlite3 が例外を投げる。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------- 開閉

    @classmethod
    def open(cls, path: Path, *, readonly: bool = True) -> SqliteStore:
        if readonly:
            found = _schema_version(path)
            if found != SCHEMA_VERSION:
                # 索引は ABR から何度でも組み直せるので移行は書かない。
                # 黙って落ちるより、何をすればよいかを言う。
                raise RuntimeError(
                    f"索引の版が合わない (索引 {found} / このパッケージ {SCHEMA_VERSION})。"
                    "build で作り直してください。"
                )
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path)
            conn.executescript(_SCHEMA)
        conn.row_factory = sqlite3.Row
        return cls(conn)

    @classmethod
    def create(cls, path: Path) -> SqliteStore:
        """書き込み用に開き、構築向けの PRAGMA を設定する。

        スキーマ版が変わっていたら作り直す。索引は ABR から何度でも組み直せる
        ので、移行を書くより捨てて作り直すほうが単純で確実。
        """
        if path.exists() and _schema_version(path) != SCHEMA_VERSION:
            path.unlink()
        store = cls.open(path, readonly=False)
        # 構築中はクラッシュしても作り直せばよいので、耐久性より速度を取る。
        store._conn.execute("PRAGMA journal_mode = OFF")
        store._conn.execute("PRAGMA synchronous = OFF")
        store._conn.execute("PRAGMA cache_size = -200000")
        store.set_meta("schema_version", str(SCHEMA_VERSION))
        return store

    def close(self) -> None:
        self._conn.close()

    def commit(self) -> None:
        self._conn.commit()

    # ------------------------------------------------------------- meta

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def meta(self) -> dict[str, str]:
        return {str(r["key"]): str(r["value"]) for r in self._conn.execute("SELECT * FROM meta")}

    # ----------------------------------------------------------- source

    def source_last_modified(self, url: str) -> str | None:
        row = self._conn.execute(
            "SELECT last_modified FROM source WHERE url = ?", (url,)
        ).fetchone()
        return None if row is None else (row["last_modified"] and str(row["last_modified"]))

    def mark_source(self, url: str, kind: str, last_modified: str | None, rows: int) -> None:
        self._conn.execute(
            "INSERT INTO source(url, kind, last_modified, rows, ingested_at) "
            "VALUES(?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(url) DO UPDATE SET "
            "  kind = excluded.kind, last_modified = excluded.last_modified,"
            "  rows = excluded.rows, ingested_at = excluded.ingested_at",
            (url, kind, last_modified, rows),
        )

    def sources(self) -> dict[str, str | None]:
        return {
            str(r["url"]): (r["last_modified"] and str(r["last_modified"]))
            for r in self._conn.execute("SELECT url, last_modified FROM source")
        }

    # ------------------------------------------------------- 書き込み

    def replace_prefs(self, records: Iterable[PrefRecord]) -> None:
        self._conn.execute("DELETE FROM pref")
        self._conn.executemany(
            "INSERT INTO pref VALUES(?, ?, ?, ?)",
            ((r.lg_code, r.pref, *_coords(r.point)) for r in records),
        )

    def replace_cities(self, records: Iterable[CityRecord]) -> None:
        self._conn.execute("DELETE FROM city")
        self._conn.executemany(
            "INSERT INTO city VALUES(?, ?, ?, ?, ?, ?, ?)",
            ((r.lg_code, r.pref, r.county, r.city, r.ward, *_coords(r.point)) for r in records),
        )

    def replace_machiaza(self, records: Iterable[MachiazaRecord]) -> None:
        self._conn.execute("DELETE FROM machiaza")
        self._conn.executemany(
            "INSERT INTO machiaza VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    r.row_id,
                    r.lg_code,
                    r.machiaza_id,
                    r.name.pref,
                    r.name.county,
                    r.name.city,
                    r.name.ward,
                    r.name.oaza_cho,
                    r.name.chome,
                    r.name.koaza,
                    r.rsdt_addr_flg,
                    *_coords(r.point),
                    r.source,
                    ",".join(str(x) for x in r.alt_machiaza),
                )
                for r in records
            ),
        )

    def put_banchi(self, items: Iterable[tuple[int, int, BanchiKind, Sequence[Banchi]]]) -> int:
        rows = [
            (lg, mz, int(kind), len(entries), banchi_codec.encode(entries))
            for lg, mz, kind, entries in items
            if entries
        ]
        self._conn.executemany(_PUT_NUMBERS, rows)
        return len(rows)

    # --------------------------------------------------------- 読み取り

    def prefs(self) -> list[PrefRecord]:
        return [
            PrefRecord(
                lg_code=int(r["lg_code"]),
                pref=str(r["pref"]),
                point=_point(r["lat_1e7"], r["lon_1e7"]),
            )
            for r in self._conn.execute("SELECT * FROM pref ORDER BY lg_code")
        ]

    def cities(self) -> list[CityRecord]:
        return [
            CityRecord(
                lg_code=int(r["lg_code"]),
                pref=str(r["pref"]),
                county=str(r["county"]),
                city=str(r["city"]),
                ward=str(r["ward"]),
                point=_point(r["lat_1e7"], r["lon_1e7"]),
            )
            for r in self._conn.execute("SELECT * FROM city ORDER BY lg_code")
        ]

    def machiaza(self, row_ids: Sequence[int]) -> dict[int, MachiazaRecord]:
        """町字を一括で引く。候補を絞ったあとにだけ呼ぶ。"""
        if not row_ids:
            return {}
        out: dict[int, MachiazaRecord] = {}
        # SQLite の変数上限 (既定 999) を避けて分割する。
        for chunk in _chunks(list(row_ids), 900):
            placeholders = ",".join("?" * len(chunk))
            for row in self._conn.execute(
                f"SELECT * FROM machiaza WHERE row_id IN ({placeholders})", chunk
            ):
                record = _town_record(row)
                out[record.row_id] = record
        return out

    def machiaza_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM machiaza").fetchone()
        return int(row["n"])

    def banchi_count(self) -> int:
        row = self._conn.execute("SELECT COALESCE(SUM(n), 0) AS n FROM num_blob").fetchone()
        return int(row["n"])

    def fetch_banchi(
        self,
        lg_code: int,
        machiaza_id: int,
        kind: BanchiKind,
        *,
        num1: int | None = None,
    ) -> list[Banchi]:
        """町字配下の番号を返す。

        ``num1`` を与えると目録で二分探索し、その番号を含むチャンクだけを展開する。
        """
        row = self._conn.execute(
            "SELECT data FROM num_blob WHERE lg_code = ? AND machiaza_id = ? AND kind = ?",
            (lg_code, machiaza_id, int(kind)),
        ).fetchone()
        if row is None:
            return []
        return banchi_codec.decode(bytes(row["data"]), num1=num1)


_PUT_NUMBERS = (
    "INSERT INTO num_blob(lg_code, machiaza_id, kind, n, data) VALUES(?, ?, ?, ?, ?) "
    "ON CONFLICT(lg_code, machiaza_id, kind) DO UPDATE SET "
    "  n = excluded.n, data = excluded.data"
)


def _schema_version(path: Path) -> int | None:
    """既存 DB のスキーマ版。読めなければ None。"""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return int(row[0]) if row else None
    except (sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        conn.close()


#: 座標を整数で持つ倍率。**層2 の BLOB と同じ値を使う** — 別にすると同じ
#: ファイルの中で座標の精度が揃わなくなる。
_SCALE = banchi_codec.COORD_SCALE


def _point(lat_1e7: Any, lon_1e7: Any) -> Point | None:
    if lat_1e7 is None or lon_1e7 is None:
        return None
    return Point(lat=int(lat_1e7) / _SCALE, lon=int(lon_1e7) / _SCALE)


def _coords(point: Point | None) -> tuple[int | None, int | None]:
    if point is None:
        return None, None
    return round(point.lat * _SCALE), round(point.lon * _SCALE)


def _town_record(row: sqlite3.Row) -> MachiazaRecord:
    return MachiazaRecord(
        row_id=int(row["row_id"]),
        lg_code=int(row["lg_code"]),
        machiaza_id=int(row["machiaza_id"]),
        name=MachiazaName(
            pref=str(row["pref"]),
            county=str(row["county"]),
            city=str(row["city"]),
            ward=str(row["ward"]),
            oaza_cho=str(row["oaza_cho"]),
            chome=str(row["chome"]),
            # 鍵を作るのにだけ要る列なので永続化していない。読み戻しでは空。
            chome_number="",
            koaza=str(row["koaza"]),
        ),
        rsdt_addr_flg=int(row["rsdt_addr_flg"]),
        point=_point(row["lat_1e7"], row["lon_1e7"]),
        source=str(row["source"]),
        alt_machiaza=_alt(row["alt_machiaza"]),
    )


def _alt(value: object) -> tuple[int, ...]:
    text = str(value or "")
    return tuple(int(x) for x in text.split(",")) if text else ()


def _chunks(items: list[int], size: int) -> Iterator[list[int]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
