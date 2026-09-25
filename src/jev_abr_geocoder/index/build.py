"""索引の構築。

ABR の CSV から ``town.marisa`` と ``abr.db`` を作る。地番まで取ると 1,887
ファイルになるため、**再開可能であること**が要件。取り込み済みのソース URL と
``Last-Modified`` を ``source`` テーブルに記録し、再実行時は変わったファイル
だけを処理する。これがそのまま差分更新になる。

ピークメモリは層2 の最大ファイル（大阪府の住居番号、約 220 万行）で 350 MB 程度。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .._version import __version__
from ..abr import csvsrc, geolonia
from ..abr.catalog import BuildLevel, FileRef, Scope, fetch_feed, kinds_for_level, select
from ..abr.fetch import Downloaded, Downloader
from ..models import NumberEntry, NumberKind, Point
from ..textnorm import normalize
from .keys import TownName, town_aliases
from .store import DB_FILENAME, SCHEMA_VERSION, Store
from .townindex import TRIE_FILENAME, build_trie

__all__ = ["BuildReport", "build", "BuildProgress"]

#: (フェーズ名, 完了, 総数, 補足) を受け取る進捗通知。
BuildProgress = Callable[[str, int, int, str], None]

#: ABR の状態フラグ。3 は取り込まない（公式実装 abr-geocoder と同じ扱い）。
_EXCLUDED_STATUS = "3"

_COORD_SCALE = 10_000_000

_KIND_TO_NUMBER: dict[str, NumberKind] = {
    "mt_rsdtdsp_blk": NumberKind.BLOCK,
    "mt_rsdtdsp_rsdt": NumberKind.RSDT,
    "mt_parcel": NumberKind.PARCEL,
}


@dataclass(slots=True)
class _TownRow:
    """``town`` テーブルの 1 行。重複をまとめる間だけ可変で持つ。"""

    town_id: int
    lg_code: int
    machiaza_id: int
    pref: str
    county: str
    city: str
    ward: str
    oaza_cho: str
    chome: str
    koaza: str
    rsdt_addr_flg: int
    lat_1e7: int | None
    lon_1e7: int | None
    source: str = "abr"

    def as_tuple(self) -> tuple[object, ...]:
        return (
            self.town_id,
            self.lg_code,
            self.machiaza_id,
            self.pref,
            self.county,
            self.city,
            self.ward,
            self.oaza_cho,
            self.chome,
            self.koaza,
            self.rsdt_addr_flg,
            self.lat_1e7,
            self.lon_1e7,
            self.source,
        )


@dataclass(slots=True)
class BuildReport:
    level: BuildLevel
    files_selected: int = 0
    files_downloaded: int = 0
    files_unchanged: int = 0
    prefs: int = 0
    cities: int = 0
    towns: int = 0
    geolonia_towns: int = 0
    trie_keys: int = 0
    numbers: int = 0
    elapsed: float = 0.0
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------- ヘルパ


def _coord(lat: str, lon: str) -> tuple[int | None, int | None]:
    if not lat or not lon:
        return None, None
    return round(float(lat) * _COORD_SCALE), round(float(lon) * _COORD_SCALE)


def _pack_key(*parts: int) -> int:
    """複数の整数 ID を 1 つの整数鍵にまとめる。位置参照の突き合わせ用。"""
    out = 0
    for part in parts:
        out = out * 1_000_000 + part
    return out


def _pair_files(downloads: Sequence[Downloaded]) -> list[tuple[Downloaded, Downloaded | None]]:
    """本体ファイルと、対応する位置参照拡張ファイルを組にする。"""
    pos_by_slot: dict[tuple[str, Scope, str], Downloaded] = {}
    for item in downloads:
        if item.ref.is_pos:
            pos_by_slot[(item.ref.text_kind, item.ref.scope, item.ref.code)] = item
    pairs: list[tuple[Downloaded, Downloaded | None]] = []
    for item in downloads:
        if item.ref.is_pos:
            continue
        slot = (item.ref.kind, item.ref.scope, item.ref.code)
        pos = pos_by_slot.get(slot)
        if pos is None:
            # 全国一括の本体に対して都道府県別の位置参照しか無い、という組み合わせ
            # は起こり得る。その場合は同じ種別・同じ範囲のものを広く探す。
            pos = next(
                (
                    p
                    for key, p in pos_by_slot.items()
                    if key[0] == item.ref.kind and key[2] == item.ref.code
                ),
                None,
            )
        pairs.append((item, pos))
    return pairs


# ------------------------------------------------------------------ 層1


def _load_town_positions(paths: Iterable[Path]) -> dict[tuple[int, int, int], tuple[int, int]]:
    """``mt_town_pos`` を (lg_code, machiaza_id, rsdt_addr_flg) で引ける形にする。"""
    out: dict[tuple[int, int, int], tuple[int, int]] = {}
    for path in paths:
        for row in csvsrc.read_rows(path):
            lat, lon = _coord(row.get("rep_lat", ""), row.get("rep_lon", ""))
            if lat is None or lon is None:
                continue
            key = (
                int(row["lg_code"]),
                int(row["machiaza_id"]),
                int(row.get("rsdt_addr_flg") or 0),
            )
            out[key] = (lat, lon)
    return out


def _load_simple_positions(paths: Iterable[Path]) -> dict[int, tuple[int, int]]:
    """``mt_pref_pos`` / ``mt_city_pos`` を lg_code で引ける形にする。"""
    out: dict[int, tuple[int, int]] = {}
    for path in paths:
        for row in csvsrc.read_rows(path):
            lat, lon = _coord(row.get("rep_lat", ""), row.get("rep_lon", ""))
            if lat is None or lon is None:
                continue
            out[int(row["lg_code"])] = (lat, lon)
    return out


def _build_prefs(store: Store, text: Sequence[Path], pos: Sequence[Path]) -> int:
    positions = _load_simple_positions(pos)
    rows: list[tuple[object, ...]] = []
    for path in text:
        for row in csvsrc.read_rows(path):
            lg_code = int(row["lg_code"])
            lat, lon = positions.get(lg_code, (None, None))
            rows.append((lg_code, row["pref"], lat, lon))
    store.replace_prefs(rows)
    return len(rows)


def _build_cities(store: Store, text: Sequence[Path], pos: Sequence[Path]) -> int:
    positions = _load_simple_positions(pos)
    rows: list[tuple[object, ...]] = []
    for path in text:
        for row in csvsrc.read_rows(path):
            if row.get("status_flg") == _EXCLUDED_STATUS:
                continue
            lg_code = int(row["lg_code"])
            lat, lon = positions.get(lg_code, (None, None))
            rows.append(
                (
                    len(rows),
                    lg_code,
                    row["pref"],
                    row.get("county", ""),
                    row.get("city", ""),
                    row.get("ward", ""),
                    lat,
                    lon,
                )
            )
    store.replace_cities(rows)
    return len(rows)


def _build_towns(
    store: Store,
    data_dir: Path,
    text: Sequence[Path],
    pos: Sequence[Path],
    geolonia_csv: Path | None = None,
) -> tuple[int, int, int]:
    """``town`` テーブルと ``town.marisa`` を作る。

    ``(町字数, 鍵数, geolonia から補った数)`` を返す。
    """
    positions = _load_town_positions(pos)
    rows: list[_TownRow] = []
    pairs: list[tuple[str, tuple[int]]] = []
    # (lg_code, machiaza_id) -> town_id。
    # mt_town は住居表示と地番の両方を持つ町字を rsdt_addr_flg 違いの 2 行で
    # 収録している（全国 727,405 行中 1,248 組）。そのまま取り込むと **表示が
    # まったく同じ選択肢を Jev に 2 つ見せる**ことになり、答えようがないので
    # 確信度が割れて粒度が落ちる。同じ町字なので 1 行にまとめる。
    by_machiaza: dict[tuple[int, int], int] = {}

    for path in text:
        for row in csvsrc.read_rows(path):
            if row.get("status_flg") == _EXCLUDED_STATUS:
                continue
            name = TownName(
                pref=row.get("pref", ""),
                county=row.get("county", ""),
                city=row.get("city", ""),
                ward=row.get("ward", ""),
                oaza_cho=row.get("oaza_cho", ""),
                chome=row.get("chome", ""),
                chome_number=row.get("chome_number", ""),
                koaza=row.get("koaza", ""),
            )
            aliases = town_aliases(name)
            if not aliases:
                continue

            lg_code = int(row["lg_code"])
            machiaza_id = int(row["machiaza_id"])
            flg = int(row.get("rsdt_addr_flg") or 0)
            coords = (
                positions.get((lg_code, machiaza_id, flg))
                or positions.get((lg_code, machiaza_id, 0))
                or positions.get((lg_code, machiaza_id, 1))
            )
            lat, lon = coords if coords else (None, None)

            slot = (lg_code, machiaza_id)
            existing = by_machiaza.get(slot)
            if existing is not None:
                # 住居表示がある側を採る。番号の取得は RSDT -> BLOCK -> PARCEL と
                # 順に試すので、1 に寄せても地番しか無い場合は拾える。
                merged = rows[existing]
                merged.rsdt_addr_flg = max(merged.rsdt_addr_flg, flg)
                if merged.lat_1e7 is None:
                    merged.lat_1e7, merged.lon_1e7 = lat, lon
                continue

            town_id = len(rows)
            by_machiaza[slot] = town_id
            rows.append(
                _TownRow(
                    town_id=town_id,
                    lg_code=lg_code,
                    machiaza_id=machiaza_id,
                    pref=name.pref,
                    county=name.county,
                    city=name.city,
                    ward=name.ward,
                    oaza_cho=name.oaza_cho,
                    chome=name.chome,
                    koaza=name.koaza,
                    rsdt_addr_flg=flg,
                    lat_1e7=lat,
                    lon_1e7=lon,
                )
            )
            payload = (town_id,)
            pairs.extend((alias, payload) for alias in aliases)

    added = _add_geolonia_towns(rows, pairs, geolonia_csv) if geolonia_csv else 0

    store.replace_towns([r.as_tuple() for r in rows])

    # サーバが読んでいる最中でも壊れないよう、一時ファイルに書いて差し替える。
    trie = build_trie(pairs)
    tmp = data_dir / f"{TRIE_FILENAME}.tmp"
    trie.save(str(tmp))
    tmp.replace(data_dir / TRIE_FILENAME)
    return len(rows), len(pairs), added


def _add_geolonia_towns(
    rows: list[_TownRow], pairs: list[tuple[str, tuple[int]]], csv_path: Path
) -> int:
    """ABR に無い町字を Geolonia 住所データから補う。

    ABR は丁目や小字を持つ大字について、**大字そのものの行を持たないことが
    ある**。「海老名市柏ケ谷」は一丁目〜六丁目しか無く、入力の番地が旧地番の
    ときに町字を決められない。実測で 3,882 件がこの型、さらに 3,246 件は
    ABR に大字ごと無い。

    補った行は ``machiaza_id`` を持たないので層2（街区・住居番号・地番）は
    引けない。町字までで止まり、残りは未解決部分として返る。
    """
    # ABR 側の照合表。ケ/ヶ の揺れは索引側エイリアスが吸収するので、
    # ここでも同じ土俵に乗せてから比べる。
    seen = {
        _match_key(r.pref + r.county + r.city + r.ward, r.oaza_cho + r.chome + r.koaza)
        for r in rows
    }
    # 市区町村ごとの lg_code。geolonia は市区町村コードを持つが、
    # ABR の lg_code とは桁が違うので名前で引く。
    lg_by_city: dict[str, int] = {}
    names_by_city: dict[str, tuple[str, str, str, str]] = {}
    for r in rows:
        city_key = _match_key(r.pref + r.county + r.city + r.ward, "")
        lg_by_city.setdefault(city_key, r.lg_code)
        names_by_city.setdefault(city_key, (r.pref, r.county, r.city, r.ward))

    added = 0
    for town in geolonia.read_rows(csv_path):
        city_key = _match_key(town.pref + town.city, "")
        lg_code = lg_by_city.get(city_key)
        if lg_code is None:
            continue
        key = _match_key(town.pref + town.city, town.town + town.koaza)
        if key in seen:
            continue
        seen.add(key)

        pref, county, city, ward = names_by_city[city_key]
        name = TownName(
            pref=pref,
            county=county,
            city=city,
            ward=ward,
            oaza_cho=town.town,
            chome="",
            chome_number="",
            koaza=town.koaza,
        )
        aliases = town_aliases(name)
        if not aliases:
            continue
        town_id = len(rows)
        rows.append(
            _TownRow(
                town_id=town_id,
                lg_code=lg_code,
                # ABR の machiaza_id を持たない。層2 は引けない。
                machiaza_id=added,
                pref=pref,
                county=county,
                city=city,
                ward=ward,
                oaza_cho=town.town,
                chome="",
                koaza=town.koaza,
                rsdt_addr_flg=0,
                lat_1e7=round(town.lat * _COORD_SCALE) if town.lat is not None else None,
                lon_1e7=round(town.lon * _COORD_SCALE) if town.lon is not None else None,
                source="geolonia",
            )
        )
        payload = (town_id,)
        pairs.extend((alias, payload) for alias in aliases)
        added += 1
    return added


#: ABR と Geolonia を突き合わせるための鍵。丁目の漢数字・算用数字の違いと
#: ケ/ヶ の揺れを吸収する。ABR は「旭ケ丘」「１丁目」、Geolonia は
#: 「旭ケ丘一丁目」のように持ち方が違う。
_KANJI_RUN = re.compile(r"([〇零一二三四五六七八九十百千]+)")
_KANA_FOLD = str.maketrans({"ヶ": "ケ", "ガ": "ケ", "が": "ケ"})


def _match_key(city: str, town: str) -> str:
    return normalize(city + _fold_numbers(town)).translate(_KANA_FOLD)


def _fold_numbers(text: str) -> str:
    """漢数字を算用数字に寄せる。突き合わせ専用。"""
    return _KANJI_RUN.sub(lambda m: str(_kanji_to_int(m.group(1))), normalize(text))


_KANJI_DIGITS = {c: i for i, c in enumerate("〇一二三四五六七八九")}


def _kanji_to_int(text: str) -> int:
    total = 0
    current = 0
    for ch in text:
        if ch in _KANJI_DIGITS:
            current = _KANJI_DIGITS[ch]
        elif ch == "十":
            total += (current or 1) * 10
            current = 0
        elif ch == "百":
            total += (current or 1) * 100
            current = 0
        elif ch == "千":
            total += (current or 1) * 1000
            current = 0
    return total + current


# ------------------------------------------------------------------ 層2


def _number_fields(kind: NumberKind, row: dict[str, str]) -> tuple[int, int, int]:
    if kind is NumberKind.BLOCK:
        return int(row.get("blk_num") or 0), 0, 0
    if kind is NumberKind.RSDT:
        return (
            int(row.get("blk_num") or 0),
            int(row.get("rsdt_num") or 0),
            int(row.get("rsdt_num2") or 0),
        )
    # 地番は prc_num* ではなく prc_id から取る。
    # 「い2」「ﾂ4」のようないろは地番があり、prc_num* は数値とは限らない
    # （鳥取県 2,137,980 筆中 8 件）。prc_id は常に 15 桁の数字で、ABR 自身が
    # それらを符号化した値を持つので、こちらを唯一の出所にする。
    prc_id = row["prc_id"]
    return int(prc_id[0:5]), int(prc_id[5:10]), int(prc_id[10:15])


def _record_key(kind: NumberKind, row: dict[str, str]) -> int:
    """本体と位置参照を突き合わせる鍵。ID 列はゼロ詰めなので整数化して使う。"""
    machiaza = int(row["machiaza_id"])
    if kind is NumberKind.BLOCK:
        return _pack_key(machiaza, int(row["blk_id"]))
    if kind is NumberKind.RSDT:
        return _pack_key(
            machiaza,
            int(row["blk_id"]),
            int(row["rsdt_id"]),
            int(row.get("rsdt2_id") or 0),
        )
    return _pack_key(machiaza, int(row["prc_id"]))


def _load_number_positions(
    kind: NumberKind, path: Path | None
) -> dict[int, dict[int, tuple[int, int]]]:
    """lg_code -> 記録鍵 -> 座標。"""
    out: dict[int, dict[int, tuple[int, int]]] = {}
    if path is None:
        return out
    for row in csvsrc.read_rows(path):
        lat, lon = _coord(row.get("rep_lat", ""), row.get("rep_lon", ""))
        if lat is None or lon is None:
            continue
        out.setdefault(int(row["lg_code"]), {})[_record_key(kind, row)] = (lat, lon)
    return out


def _ingest_numbers(store: Store, kind: NumberKind, text_path: Path, pos_path: Path | None) -> int:
    """1 ファイル分の番号を町字単位の BLOB にして書き込む。"""
    positions = _load_number_positions(kind, pos_path)

    # (lg_code, machiaza_id) -> [(num1, num2, num3, lat, lon), ...]
    groups: dict[tuple[int, int], list[tuple[int, int, int, int | None, int | None]]] = {}
    for row in csvsrc.read_rows(text_path):
        if row.get("status_flg") == _EXCLUDED_STATUS:
            continue
        lg_code = int(row["lg_code"])
        machiaza_id = int(row["machiaza_id"])
        num1, num2, num3 = _number_fields(kind, row)
        coords = positions.get(lg_code, {}).get(_record_key(kind, row))
        lat, lon = coords if coords else (None, None)
        groups.setdefault((lg_code, machiaza_id), []).append((num1, num2, num3, lat, lon))

    del positions

    written = 0
    batch: list[tuple[int, int, NumberKind, Sequence[NumberEntry]]] = []
    for (lg_code, machiaza_id), raw in groups.items():
        entries = [
            NumberEntry(
                num1=num1,
                num2=num2,
                num3=num3,
                point=None
                if lat is None or lon is None
                else Point(lat=lat / _COORD_SCALE, lon=lon / _COORD_SCALE),
            )
            for num1, num2, num3, lat, lon in raw
        ]
        batch.append((lg_code, machiaza_id, kind, entries))
        written += len(entries)
        if len(batch) >= 512:
            store.put_numbers_many(batch)
            batch.clear()
    if batch:
        store.put_numbers_many(batch)
    return written


# ------------------------------------------------------------- 構築本体


async def build(
    data_dir: Path,
    level: BuildLevel = BuildLevel.MACHIAZA,
    *,
    prefs: Sequence[str] | None = None,
    cities: Sequence[str] | None = None,
    concurrency: int = 4,
    force: bool = False,
    with_geolonia: bool = True,
    progress: BuildProgress | None = None,
    refs: Sequence[FileRef] | None = None,
) -> BuildReport:
    """ABR を取り込んで索引を作る。

    ``force`` が False なら、``Last-Modified`` が変わっていないファイルは
    層2 の取り込みを飛ばす。層1 は安いので毎回作り直す。

    ``with_geolonia`` が True なら、ABR に無い町字を Geolonia 住所データ
    (CC BY 4.0) で補う。帰属表示が要るので ``meta`` に記録する。
    """
    started = time.monotonic()
    data_dir.mkdir(parents=True, exist_ok=True)
    report = BuildReport(level=level)

    def notify(phase: str, done: int, total: int, detail: str = "") -> None:
        if progress is not None:
            progress(phase, done, total, detail)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        if refs is None:
            notify("catalog", 0, 1, "DCAT フィードを取得中")
            refs = await fetch_feed(client)
            notify("catalog", 1, 1, f"{len(refs):,} ファイル")

        selected = select(refs, kinds_for_level(level), prefs=prefs, cities=cities)
        report.files_selected = len(selected)
        if not selected:
            report.notes.append("該当するファイルが無い")
            report.elapsed = time.monotonic() - started
            return report

        downloader = Downloader(data_dir / "cache", concurrency=concurrency, client=client)
        downloads = await downloader.download_all(
            selected,
            progress=lambda done, total, name: notify("download", done, total, name),
        )

    report.files_downloaded = sum(1 for d in downloads if not d.cached)
    report.files_unchanged = sum(1 for d in downloads if d.cached)

    by_kind: dict[str, list[Downloaded]] = {}
    for item in downloads:
        by_kind.setdefault(item.ref.kind, []).append(item)

    with Store.create(data_dir / DB_FILENAME) as store:
        store.set_meta("schema_version", str(SCHEMA_VERSION))
        store.set_meta("builder_version", __version__)
        store.set_meta("level", level.value)

        if "mt_pref" in by_kind:
            notify("layer1", 0, 3, "都道府県")
            report.prefs = _build_prefs(
                store,
                [d.path for d in by_kind["mt_pref"]],
                [d.path for d in by_kind.get("mt_pref_pos", [])],
            )
        if "mt_city" in by_kind:
            notify("layer1", 1, 3, "市区町村")
            report.cities = _build_cities(
                store,
                [d.path for d in by_kind["mt_city"]],
                [d.path for d in by_kind.get("mt_city_pos", [])],
            )
        if "mt_town" in by_kind:
            geolonia_csv: Path | None = None
            if with_geolonia:
                notify("geolonia", 0, 1, "住所データを取得中")
                try:
                    geolonia_csv = geolonia.download(data_dir / "cache")
                    notify("geolonia", 1, 1, geolonia_csv.name)
                except Exception as exc:  # noqa: BLE001 - 補完なので失敗しても続ける
                    report.notes.append(f"Geolonia 住所データを取得できなかった: {exc}")
            notify("layer1", 2, 3, "町字とトライ")
            report.towns, report.trie_keys, report.geolonia_towns = _build_towns(
                store,
                data_dir,
                [d.path for d in by_kind["mt_town"]],
                [d.path for d in by_kind.get("mt_town_pos", [])],
                geolonia_csv,
            )
            if report.geolonia_towns:
                store.set_meta("geolonia_attribution", geolonia.ATTRIBUTION)
                store.set_meta("geolonia_towns", str(report.geolonia_towns))
        notify("layer1", 3, 3, "完了")
        store.commit()

        number_pairs = [
            (text, pos) for text, pos in _pair_files(downloads) if text.ref.kind in _KIND_TO_NUMBER
        ]
        for index, (text, pos) in enumerate(number_pairs):
            kind = _KIND_TO_NUMBER[text.ref.kind]
            known = store.source_last_modified(text.ref.url)
            unchanged = (
                not force
                and known is not None
                and known == text.last_modified
                and text.last_modified is not None
            )
            if unchanged:
                notify("layer2", index + 1, len(number_pairs), f"{text.ref.filename} (変更なし)")
                continue
            notify("layer2", index + 1, len(number_pairs), text.ref.filename)
            written = _ingest_numbers(store, kind, text.path, pos.path if pos else None)
            report.numbers += written
            store.mark_source(text.ref.url, text.ref.kind, text.last_modified, written)
            store.commit()

        for item in downloads:
            if item.ref.kind in {"mt_pref", "mt_city", "mt_town"}:
                store.mark_source(item.ref.url, item.ref.kind, item.last_modified, 0)
        store.set_meta("built_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
        store.commit()

    report.elapsed = time.monotonic() - started
    return report
