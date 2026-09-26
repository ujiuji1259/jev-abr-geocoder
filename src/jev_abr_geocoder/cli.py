"""コマンドライン。

コアの薄いアダプタでしかない。将来の HTTP サーバも同じ
:meth:`Geocoder.geocode_many` を呼ぶだけになる。

``normalize`` は標準入力をバッチにまとめて渡す。1 行ずつ ``geocode`` を呼ぶ
実装にしてはならない（docs/code-design.md 制約1）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import closing
from pathlib import Path
from typing import Annotated

import typer

from . import adapters, ports
from ._version import __version__
from .config import GeocoderConfig
from .geocoder import Geocoder
from .outcome import GeocodeResult

app = typer.Typer(
    add_completion=False,
    help="ABR を trie で前方一致させ、候補を Jev に選ばせる住所正規化・ジオコーダ",
)

_DEFAULT_DATA_DIR = Path(os.environ.get("JEV_ABR_DATA_DIR", "data"))

DataDir = Annotated[Path, typer.Option("--data-dir", "-d", help="索引の置き場 (既定: ./data)")]


@app.command()
def build(
    level: Annotated[
        str, typer.Option("--level", "-l", help="machiaza | rsdt | parcel")
    ] = "machiaza",
    data_dir: DataDir = _DEFAULT_DATA_DIR,
    pref: Annotated[
        list[str] | None, typer.Option("--pref", help="都道府県コード 2 桁。複数可")
    ] = None,
    city: Annotated[
        list[str] | None, typer.Option("--city", help="市区町村コード 6 桁。複数可")
    ] = None,
    concurrency: Annotated[int, typer.Option(help="同時ダウンロード数")] = 4,
    force: Annotated[bool, typer.Option("--force", help="変更が無くても再取り込み")] = False,
    geolonia: Annotated[
        bool,
        typer.Option(
            "--geolonia/--no-geolonia",
            help="ABR に無い町字を Geolonia 住所データ (CC BY 4.0) で補う",
        ),
    ] = True,
) -> None:
    """ABR を取り込んで索引を作る。再実行すると差分だけを取り込む。"""
    from .abr.catalog import BuildLevel
    from .index.build import build as run_build

    try:
        build_level = BuildLevel(level)
    except ValueError:
        raise typer.BadParameter("machiaza / rsdt / parcel のいずれか") from None

    def progress(phase: str, done: int, total: int, detail: str) -> None:
        typer.echo(f"[{phase}] {done}/{total} {detail}", err=True)

    report = asyncio.run(
        run_build(
            data_dir,
            build_level,
            prefs=pref or None,
            cities=city or None,
            concurrency=concurrency,
            force=force,
            with_geolonia=geolonia,
            progress=progress,
        )
    )
    typer.echo(
        "\n".join(
            [
                f"深さ          : {report.level.value}",
                f"対象ファイル  : {report.files_selected} "
                f"(新規取得 {report.files_downloaded} / 変更なし {report.files_unchanged})",
                f"都道府県      : {report.prefs:,}",
                f"市区町村      : {report.cities:,}",
                f"町字          : {report.towns:,}  (索引鍵 {report.trie_keys:,})",
                f"  ABR 内で畳んだ: {report.machiaza_folded:,}",
                f"  うち Geolonia: {report.geolonia_added:,} "
                f"(既存に畳んだ別名 {report.geolonia_merged:,})",
                f"番号          : {report.banchi:,}",
                f"所要          : {report.elapsed:.1f} 秒",
            ]
            + [f"注記          : {note}" for note in report.notes]
        )
    )


@app.command()
def normalize(
    address: Annotated[list[str] | None, typer.Argument(help="住所。省略で標準入力")] = None,
    data_dir: DataDir = _DEFAULT_DATA_DIR,
    jsonl: Annotated[bool, typer.Option("--jsonl", help="JSON Lines で出力")] = False,
    batch_size: Annotated[int, typer.Option(help="1 リクエストにまとめる件数")] = 64,
    concurrency: Annotated[
        int, typer.Option(help="並行して走らせるバッチ数。処理時間は Jev の応答待ちが支配的")
    ] = 4,
    no_model: Annotated[
        bool, typer.Option("--no-model", help="Jev を呼ばず、トライの候補だけで判定する")
    ] = False,
    always_ask: Annotated[
        bool, typer.Option("--always-ask", help="ファストパスを無効化して常に Jev を通す")
    ] = False,
) -> None:
    """住所を正規化する。"""
    cfg = GeocoderConfig(batch_size=batch_size, concurrency=concurrency, always_ask=always_ask)
    queries = list(address) if address else [line.strip() for line in sys.stdin if line.strip()]
    if not queries:
        raise typer.BadParameter("住所が指定されていない")

    model = None if no_model else _open_model(cfg)
    with Geocoder.open(data_dir, model=model, cfg=cfg) as geocoder:
        # 区切りと並行化は Geocoder が面倒を見る。
        results = asyncio.run(geocoder.geocode_many(queries))

    for result in results:
        if jsonl:
            typer.echo(json.dumps(result.to_dict(), ensure_ascii=False))
        else:
            typer.echo(_human(result))


@app.command()
def info(data_dir: DataDir = _DEFAULT_DATA_DIR) -> None:
    """索引の版・件数・取得日時を表示する。"""
    db = adapters.index_path(data_dir)
    if not db.exists():
        typer.echo(f"索引が無い: {db}", err=True)
        raise typer.Exit(1)
    with closing(adapters.open_reader(data_dir)) as store:
        meta = store.meta()
        lines = [f"パッケージ  : {__version__}", f"索引        : {data_dir}"]
        for key in ("schema_version", "builder_version", "level", "built_at"):
            if key in meta:
                lines.append(f"{key:12}: {meta[key]}")
        lines.append(f"町字        : {store.machiaza_count():,}")
        lines.append(f"番号        : {store.banchi_count():,}")
        lines.append(f"取り込み済み: {len(store.sources()):,} ファイル")
        if "geolonia_attribution" in meta:
            lines.append("")
            lines.append(meta["geolonia_attribution"])
    typer.echo("\n".join(lines))


# ------------------------------------------------------------------ 補助


def _open_model(cfg: GeocoderConfig) -> ports.DecisionModel | None:
    if not os.environ.get("TYPESAFE_API_KEY"):
        typer.echo(
            "TYPESAFE_API_KEY が未設定のため、Jev を使わずトライの候補だけで判定する", err=True
        )
        return None
    return adapters.jev_model(cfg)


def _human(result: GeocodeResult) -> str:
    head = result.address or "(解決できず)"
    bits = [f"{result.query}  ->  {head}", f"  粒度      : {result.granularity.label}"]
    if result.lat is not None and result.lon is not None:
        bits.append(f"  座標      : {result.lat:.6f}, {result.lon:.6f}")
    codes = [
        ("lg_code", result.lg_code),
        ("machiaza_id", result.machiaza_id),
        ("blk_id", result.blk_id),
        ("rsdt_id", result.rsdt_id),
        ("prc_id", result.prc_id),
    ]
    filled = [f"{name}={value}" for name, value in codes if value]
    if filled:
        bits.append("  ABR       : " + " ".join(filled))
    if result.remainder:
        bits.append(f"  残り      : {result.remainder}")
    bits.append(f"  確信度    : {result.confidence:.2f}  resolved={result.resolved}")
    if result.note:
        bits.append(f"  注記      : {result.note}")
    return "\n".join(bits)


if __name__ == "__main__":  # pragma: no cover
    app()
