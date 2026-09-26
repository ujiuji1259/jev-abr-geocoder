"""SQLite ストア。

1 ファイルに層1 のレコード (``pref`` / ``city`` / ``town``) と層2 の番号 BLOB
(``num_blob``) が入る。配布物は ``town.marisa`` とこの ``abr.db`` の 2 つだけ。

読み取りは読み取り専用接続で開くので、複数ワーカーから安全に共有できる。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

from ..models import CityRecord, NumberEntry, NumberKind, Point, PrefRecord, TownRecord
from . import numblob

__all__ = ["Store", "SCHEMA_VERSION", "DB_FILENAME"]

SCHEMA_VERSION = 3
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
    city_id INTEGER PRIMARY KEY,
    lg_code INTEGER NOT NULL UNIQUE,
    pref    TEXT NOT NULL,
    county  TEXT NOT NULL,
    city    TEXT NOT NULL,
    ward    TEXT NOT NULL,
    lat_1e7 INTEGER,
    lon_1e7 INTEGER
);

-- town_id は marisa トライのペイロードと一致する。
CREATE TABLE IF NOT EXISTS town(
    town_id       INTEGER PRIMARY KEY,
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
CREATE UNIQUE INDEX IF NOT EXISTS town_by_machiaza ON town(lg_code, machiaza_id, source);

-- 街区・住居番号・地番。町字単位でパックした BLOB（numblob.py の形式）。
CREATE TABLE IF NOT EXISTS num_blob(
    lg_code     INTEGER NOT NULL,
    machiaza_id INTEGER NOT NULL,
    kind        INTEGER NOT NULL,
    n           INTEGER NOT NULL,
    data        BLOB NOT NULL,
    PRIMARY KEY(lg_code, machiaza_id, kind)
) WITHOUT ROWID;
"""


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


def _point(lat_1e7: int | None, lon_1e7: int | None) -> Point | None:
    if lat_1e7 is None or lon_1e7 is None:
        return None
    return Point(lat=lat_1e7 / numblob.COORD_SCALE, lon=lon_1e7 / numblob.COORD_SCALE)


class Store:
    """``abr.db`` への読み書き。

    読み取り専用で開いた場合、書き込み系メソッドは sqlite3 が例外を投げる。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------- 開閉

    @classmethod
    def open(cls, path: Path, *, readonly: bool = True) -> Store:
        if readonly:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path)
            conn.executescript(_SCHEMA)
        conn.row_factory = sqlite3.Row
        return cls(conn)

    @classmethod
    def create(cls, path: Path) -> Store:
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
        return store

    def close(self) -> None:
        self._conn.close()

    def commit(self) -> None:
        self._conn.commit()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- meta

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def all_meta(self) -> dict[str, str]:
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

    def ingested_sources(self) -> dict[str, str | None]:
        return {
            str(r["url"]): (r["last_modified"] and str(r["last_modified"]))
            for r in self._conn.execute("SELECT url, last_modified FROM source")
        }

    # ------------------------------------------------------- 書き込み

    def replace_prefs(self, rows: Iterable[tuple[Any, ...]]) -> None:
        self._conn.execute("DELETE FROM pref")
        self._conn.executemany("INSERT INTO pref VALUES(?, ?, ?, ?)", rows)

    def replace_cities(self, rows: Iterable[tuple[Any, ...]]) -> None:
        self._conn.execute("DELETE FROM city")
        self._conn.executemany("INSERT INTO city VALUES(?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def replace_towns(self, rows: Iterable[Sequence[Any]]) -> None:
        self._conn.execute("DELETE FROM town")
        self._conn.executemany(
            "INSERT INTO town VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
        )

    def put_numbers(
        self, lg_code: int, machiaza_id: int, kind: NumberKind, entries: Sequence[NumberEntry]
    ) -> None:
        if not entries:
            return
        self._conn.execute(
            "INSERT INTO num_blob(lg_code, machiaza_id, kind, n, data) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(lg_code, machiaza_id, kind) DO UPDATE SET "
            "  n = excluded.n, data = excluded.data",
            (lg_code, machiaza_id, int(kind), len(entries), numblob.encode(entries)),
        )

    def put_numbers_many(
        self, items: Iterable[tuple[int, int, NumberKind, Sequence[NumberEntry]]]
    ) -> int:
        rows = [
            (lg, mz, int(kind), len(entries), numblob.encode(entries))
            for lg, mz, kind, entries in items
            if entries
        ]
        self._conn.executemany(
            "INSERT INTO num_blob(lg_code, machiaza_id, kind, n, data) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(lg_code, machiaza_id, kind) DO UPDATE SET "
            "  n = excluded.n, data = excluded.data",
            rows,
        )
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
                city_id=int(r["city_id"]),
                lg_code=int(r["lg_code"]),
                pref=str(r["pref"]),
                county=str(r["county"]),
                city=str(r["city"]),
                ward=str(r["ward"]),
                point=_point(r["lat_1e7"], r["lon_1e7"]),
            )
            for r in self._conn.execute("SELECT * FROM city ORDER BY city_id")
        ]

    def towns(self, town_ids: Sequence[int]) -> dict[int, TownRecord]:
        """町字を一括で引く。候補を絞ったあとにだけ呼ぶ。"""
        if not town_ids:
            return {}
        out: dict[int, TownRecord] = {}
        # SQLite の変数上限 (既定 999) を避けて分割する。
        for chunk in _chunks(list(town_ids), 900):
            placeholders = ",".join("?" * len(chunk))
            for row in self._conn.execute(
                f"SELECT * FROM town WHERE town_id IN ({placeholders})", chunk
            ):
                record = _town_record(row)
                out[record.town_id] = record
        return out

    def town(self, town_id: int) -> TownRecord | None:
        row = self._conn.execute("SELECT * FROM town WHERE town_id = ?", (town_id,)).fetchone()
        return None if row is None else _town_record(row)

    def town_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM town").fetchone()
        return int(row["n"])

    def number_count(self) -> int:
        row = self._conn.execute("SELECT COALESCE(SUM(n), 0) AS n FROM num_blob").fetchone()
        return int(row["n"])

    def fetch_numbers(
        self,
        lg_code: int,
        machiaza_id: int,
        kind: NumberKind,
        *,
        num1: int | None = None,
    ) -> list[NumberEntry]:
        """町字配下の番号を返す。

        ``num1`` を与えると目録で二分探索し、その番号を含むチャンクだけを展開する。
        """
        row = self._conn.execute(
            "SELECT data FROM num_blob WHERE lg_code = ? AND machiaza_id = ? AND kind = ?",
            (lg_code, machiaza_id, int(kind)),
        ).fetchone()
        if row is None:
            return []
        return numblob.decode(bytes(row["data"]), num1=num1)

    def has_numbers(self, lg_code: int, machiaza_id: int, kind: NumberKind) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM num_blob WHERE lg_code = ? AND machiaza_id = ? AND kind = ?",
            (lg_code, machiaza_id, int(kind)),
        ).fetchone()
        return row is not None


def _town_record(row: sqlite3.Row) -> TownRecord:
    return TownRecord(
        town_id=int(row["town_id"]),
        lg_code=int(row["lg_code"]),
        machiaza_id=int(row["machiaza_id"]),
        pref=str(row["pref"]),
        county=str(row["county"]),
        city=str(row["city"]),
        ward=str(row["ward"]),
        oaza_cho=str(row["oaza_cho"]),
        chome=str(row["chome"]),
        koaza=str(row["koaza"]),
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
