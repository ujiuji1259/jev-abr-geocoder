"""座標の検証 — 位置参照情報と突き合わせる（docs/eval.md §13）。

国土交通省「位置参照情報」の大字・町丁目レベルは、**ABR とは別系統で整備された
住所＋緯度経度**。``lg_code`` と ``machiaza_id`` が合っていても代表点が別の場所を
指していたら地図では使えないので、文字列の一致とは独立に座標を測る。

測るもの:

- **解決率** — 位置参照情報の住所表記で町字まで解決できるか（別表記への耐性）
- **距離** — こちらが返した代表点と、位置参照情報の代表点の隔たり [m]

ずれが大きい件は、ABR の代表点の欠損（市区町村の代表点で代用している）か、
町字の取り違えのどちらか。前者は ``machiaza_id`` が合っていてもずれるので、
この評価でしか見えない。

データはコミットしない（利用約款の確認が要るため）。取得はこのスクリプトが行い、
``data/cache/eval/`` に置く（``data/`` は .gitignore 済み）。

    uv run python scripts/eval_coords.py --pref 31
    uv run python scripts/eval_coords.py --pref 31 --pref 13 --model
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import math
import sys
import time
import urllib.request
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jev_abr_geocoder import Geocoder, GeocoderConfig, adapters  # noqa: E402
from jev_abr_geocoder.address import Point  # noqa: E402

VERSION = "19.0b"
BASE_URL = "https://nlftp.mlit.go.jp/isj/dls/data"
SOURCE = "位置参照情報（国土交通省）大字・町丁目レベル"


@dataclass(frozen=True, slots=True)
class Case:
    pref: str
    city: str
    town: str
    point: Point

    @property
    def query(self) -> str:
        return f"{self.pref}{self.city}{self.town}"


def download(cache_dir: Path, pref: int) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    name = f"{pref:02d}000-{VERSION}.zip"
    path = cache_dir / name
    if path.exists():
        return path
    url = f"{BASE_URL}/{VERSION}/{name}"
    print(f"取得中: {url}")
    with urllib.request.urlopen(url) as response:  # noqa: S310 - 固定のホスト
        tmp = path.with_suffix(".part")
        tmp.write_bytes(response.read())
        tmp.replace(path)
    return path


def read_cases(path: Path) -> list[Case]:
    """zip 内の CSV を読む。Shift_JIS、1 行目はヘッダ。"""
    out: list[Case] = []
    with zipfile.ZipFile(path) as archive:
        name = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
        with archive.open(name) as raw:
            stream = io.TextIOWrapper(raw, encoding="cp932", newline="")
            for row in csv.DictReader(stream):
                town = (row.get("大字町丁目名") or "").strip()
                lat, lon = row.get("緯度"), row.get("経度")
                if not town or not lat or not lon:
                    continue
                out.append(
                    Case(
                        pref=(row.get("都道府県名") or "").strip(),
                        city=(row.get("市区町村名") or "").strip(),
                        town=town,
                        point=Point(lat=float(lat), lon=float(lon)),
                    )
                )
    return out


def distance_m(a: Point, b: Point) -> float:
    """2 点間の距離 [m]。測地系の違い（JGD2011 と ABR の EPSG:6668）は
    同一とみなす — 差は 1 m 未満で、ここで見たいずれは 100 m 単位。"""
    radius = 6_371_000.0
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat, dlon = lat2 - lat1, math.radians(b.lon - a.lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    index = min(len(values) - 1, int(len(values) * q))
    return sorted(values)[index]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--pref", type=int, action="append", required=True, help="都道府県コード")
    parser.add_argument("--limit", type=int, help="先頭 N 件だけ")
    parser.add_argument("--model", action="store_true", help="Jev を使う")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()

    cases: list[Case] = []
    for pref in args.pref:
        cases.extend(read_cases(download(args.data_dir / "cache" / "eval", pref)))
    print(f"{SOURCE}: {len(cases):,} 件")
    if args.limit:
        cases = cases[: args.limit]

    cfg = GeocoderConfig(batch_size=args.batch_size, concurrency=args.concurrency)
    model = adapters.jev_model(cfg) if args.model else None
    started = time.perf_counter()
    with Geocoder.open(args.data_dir, model=model, cfg=cfg) as geocoder:
        outcome = await geocoder.run_all([case.query for case in cases])
    elapsed = time.perf_counter() - started

    distances: list[float] = []
    levels: Counter[str] = Counter()
    far: list[tuple[Case, str, float]] = []
    no_point = 0
    for case, result in zip(cases, outcome.results, strict=True):
        levels[result.granularity.label] += 1
        if result.point is None:
            no_point += 1
            continue
        d = distance_m(case.point, result.point)
        distances.append(d)
        if d > 1000 and len(far) < 10:
            far.append((case, result.address, d))

    n = len(cases)
    print(f"\n入力 {n:,} 件  所要 {elapsed:.1f} 秒 ({elapsed / n * 1000:.2f} ms/件)")
    print(f"Jev 往復 {outcome.usage.requests}")
    print("\n粒度の分布")
    for label, count in levels.most_common():
        print(f"  {label:8} {count:>7,}  {count / n:.1%}")
    print(f"\n座標を返せた   {len(distances):,} / {n:,}  ({len(distances) / n:.1%})")
    if distances:
        print("位置参照情報との隔たり [m]")
        for label, q in (("p50", 0.50), ("p90", 0.90), ("p99", 0.99)):
            print(f"  {label}  {_percentile(distances, q):>10,.0f}")
        print(f"  max  {max(distances):>10,.0f}")
        for threshold in (200, 500, 1000, 5000):
            over = sum(1 for d in distances if d > threshold)
            print(f"  {threshold:>5} m 超  {over:>6,}  {over / len(distances):.1%}")
    if far:
        print("\n1 km 以上ずれた例")
        for case, address, d in far:
            print(f"  {d:>7,.0f} m  {case.query:28} -> {address}")


if __name__ == "__main__":
    asyncio.run(main())
