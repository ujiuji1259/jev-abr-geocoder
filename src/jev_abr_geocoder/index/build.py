"""索引の構築。

ABR の CSV から ``town.marisa`` と ``abr.db`` を作る。地番まで取ると 1,887
ファイルになるため、**再開可能であること**が要件。取り込み済みのソース URL と
``Last-Modified`` を ``source`` テーブルに記録し、再実行時は変わったファイル
だけを処理する。これがそのまま差分更新になる。

ここにあるのは段取りだけ。CSV の列の読み方は :mod:`abr.rows`、町字の畳み込みは
:mod:`index.machiaza_table` にある。

ピークメモリは層2 の最大ファイル（大阪府の住居番号、約 220 万行）で 350 MB 程度。

:func:`build` は**構築側の合成の根**で、省略された外部依存（HTTP・トライ・
永続化）に既定のアダプタを当てる。それ以外の関数はポートしか知らない。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .. import ports
from .._version import __version__
from ..abr import geolonia, rows
from ..abr.catalog import BuildLevel, FileRef, Scope, fetch_feed, kinds_for_level, select
from ..abr.fetch import Downloaded, Downloader
from ..address import Banchi, BanchiKind, CityRecord
from .machiaza_table import MachiazaStats, MachiazaTable

__all__ = ["BuildReport", "build", "BuildProgress"]

#: (フェーズ名, 完了, 総数, 補足) を受け取る進捗通知。
BuildProgress = Callable[[str, int, int, str], None]

_KIND_TO_BANCHI: dict[str, BanchiKind] = {
    "mt_rsdtdsp_blk": BanchiKind.BLOCK,
    "mt_rsdtdsp_rsdt": BanchiKind.RSDT,
    "mt_parcel": BanchiKind.PARCEL,
}


@dataclass(slots=True)
class BuildReport:
    level: BuildLevel
    files_selected: int = 0
    files_downloaded: int = 0
    files_unchanged: int = 0
    prefs: int = 0
    cities: int = 0
    towns: int = 0
    geolonia_added: int = 0
    #: ABR に既にあったので行を足さず別名だけ足した Geolonia の町字。
    geolonia_merged: int = 0
    #: 大字・字の違いだけの別レコードとしてまとめた ABR の町字。
    machiaza_folded: int = 0
    trie_keys: int = 0
    banchi: int = 0
    elapsed: float = 0.0
    notes: list[str] = field(default_factory=list)

    def apply(self, stats: MachiazaStats) -> None:
        self.towns = stats.towns
        self.trie_keys = stats.trie_keys
        self.machiaza_folded = stats.folded
        self.geolonia_added = stats.geolonia_added
        self.geolonia_merged = stats.geolonia_merged


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


def _build_prefs(writer: ports.IndexWriter, text: Sequence[Path], pos: Sequence[Path]) -> int:
    records = list(rows.read_prefs(text, rows.read_simple_positions(pos)))
    writer.replace_prefs(records)
    return len(records)


def _build_cities(writer: ports.IndexWriter, text: Sequence[Path], pos: Sequence[Path]) -> int:
    records = [
        CityRecord(
            lg_code=row.lg_code,
            pref=row.pref,
            county=row.county,
            city=row.city,
            ward=row.ward,
            point=row.point,
        )
        for row in rows.read_cities(text, rows.read_simple_positions(pos))
    ]
    writer.replace_cities(records)
    return len(records)


def _build_machiaza(
    writer: ports.IndexWriter,
    backend: ports.TrieBackend,
    trie_path: Path,
    text: Sequence[Path],
    pos: Sequence[Path],
    geolonia_csv: Path | None = None,
) -> MachiazaStats:
    """``town`` テーブルと前方一致トライを作る。どちらも毎回作り直す。"""
    table = MachiazaTable()
    table.add_abr(rows.read_machiaza(text, rows.read_town_positions(pos)))
    if geolonia_csv is not None:
        table.add_geolonia(geolonia.read_rows(geolonia_csv))
    writer.replace_machiaza(table.records)
    backend.save(table.pairs, trie_path)
    return table.stats


# ------------------------------------------------------------------ 層2


def _ingest_banchi(
    writer: ports.IndexWriter, kind: BanchiKind, text_path: Path, pos_path: Path | None
) -> int:
    """1 ファイル分の番号を町字単位の BLOB にして書き込む。"""
    positions = rows.read_banchi_positions(kind, pos_path)

    groups: dict[tuple[int, int], list[Banchi]] = {}
    for row in rows.read_banchi(kind, text_path):
        groups.setdefault((row.lg_code, row.machiaza_id), []).append(
            Banchi(*row.nums, point=positions.get(row.lg_code, {}).get(row.record_key))
        )
    del positions

    written = 0
    batch: list[tuple[int, int, BanchiKind, Sequence[Banchi]]] = []
    for (lg_code, machiaza_id), entries in groups.items():
        batch.append((lg_code, machiaza_id, kind, entries))
        written += len(entries)
        if len(batch) >= 512:
            writer.put_banchi(batch)
            batch.clear()
    if batch:
        writer.put_banchi(batch)
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
    http: ports.HttpClient | None = None,
    trie_backend: ports.TrieBackend | None = None,
    writer: ports.IndexWriter | None = None,
    trie_path: Path | None = None,
) -> BuildReport:
    """ABR を取り込んで索引を作る。

    ``force`` が False なら、``Last-Modified`` が変わっていないファイルは
    層2 の取り込みを飛ばす。層1 は安いので毎回作り直す。

    ``with_geolonia`` が True なら、ABR に無い町字を Geolonia 住所データ
    (CC BY 4.0) で補う。帰属表示が要るので ``meta`` に記録する。

    ``http`` / ``trie_backend`` / ``writer`` / ``trie_path`` を省略すると既定の
    アダプタを使う。**ここが構築側の合成の根**で、差し替えたい呼び出し側
    （テストや別バックエンド）は同じポートを満たすものを渡せばよい。
    """
    from .. import adapters

    started = time.monotonic()
    data_dir.mkdir(parents=True, exist_ok=True)
    report = BuildReport(level=level)

    def notify(phase: str, done: int, total: int, detail: str = "") -> None:
        if progress is not None:
            progress(phase, done, total, detail)

    backend = trie_backend or adapters.trie_backend()
    target = trie_path or adapters.trie_path(data_dir)
    client = http or adapters.http_client()
    try:
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

        downloader = Downloader(data_dir / "cache", client, concurrency=concurrency)
        downloads = await downloader.download_all(
            selected,
            progress=lambda done, total, name: notify("download", done, total, name),
        )

        geolonia_csv: Path | None = None
        if with_geolonia and "mt_town" in {d.ref.kind for d in downloads}:
            notify("geolonia", 0, 1, "住所データを取得中")
            try:
                geolonia_csv = await geolonia.download(data_dir / "cache", client)
                notify("geolonia", 1, 1, geolonia_csv.name)
            except Exception as exc:  # noqa: BLE001 - 補完なので失敗しても続ける
                report.notes.append(f"Geolonia 住所データを取得できなかった: {exc}")
    finally:
        if http is None:
            await client.close()

    report.files_downloaded = sum(1 for d in downloads if not d.cached)
    report.files_unchanged = sum(1 for d in downloads if d.cached)

    by_kind: dict[str, list[Downloaded]] = {}
    for item in downloads:
        by_kind.setdefault(item.ref.kind, []).append(item)

    store = writer or adapters.create_writer(data_dir)
    try:
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
            notify("layer1", 2, 3, "町字とトライ")
            report.apply(
                _build_machiaza(
                    store,
                    backend,
                    target,
                    [d.path for d in by_kind["mt_town"]],
                    [d.path for d in by_kind.get("mt_town_pos", [])],
                    geolonia_csv,
                )
            )
            if report.geolonia_added or report.geolonia_merged:
                store.set_meta("geolonia_attribution", geolonia.ATTRIBUTION)
                store.set_meta("geolonia_added", str(report.geolonia_added))
                store.set_meta("geolonia_merged", str(report.geolonia_merged))
        notify("layer1", 3, 3, "完了")
        store.commit()

        number_pairs = [
            (text, pos) for text, pos in _pair_files(downloads) if text.ref.kind in _KIND_TO_BANCHI
        ]
        for index, (text, pos) in enumerate(number_pairs):
            kind = _KIND_TO_BANCHI[text.ref.kind]
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
            written = _ingest_banchi(store, kind, text.path, pos.path if pos else None)
            report.banchi += written
            store.mark_source(text.ref.url, text.ref.kind, text.last_modified, written)
            store.commit()

        for item in downloads:
            if item.ref.kind in {"mt_pref", "mt_city", "mt_town"}:
                store.mark_source(item.ref.url, item.ref.kind, item.last_modified, 0)
        store.set_meta("built_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
        store.commit()
    finally:
        if writer is None:
            store.close()

    report.elapsed = time.monotonic() - started
    return report
