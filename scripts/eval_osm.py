"""OpenStreetMap との突き合わせ（docs/eval.md §14）。

町字の座標を**出所の違う相手**と比べる唯一の手。位置参照情報（§13）は ABR の
代表点と出所が同じで隔たりが 0 m になってしまうが、OSM は現地調査・航空写真の
トレースなので測量の系統が別。

**評価にしか使わない。** OSM は ODbL 1.0 なので、座標を索引に取り込むと派生
データベースになって継承義務が索引全体に及ぶ。集計値を出すだけなら派生物の配布に
当たらないので問題にならない。データもコミットしない（``data/cache/eval/``）。

**距離の読み方に注意。** OSM の place ノードは人が「この辺」と置いた点で、代表点の
計算方法が決まっているわけではない。数百 m の差は正常。この評価で見えるのは
**町字の取り違えと座標の壊れ**（1 km 以上）で、メートル単位の精度ではない。

番号（街区・住居番号・地番）には使えない。鳥取市で実測すると ABR 18,739 件の
住居番号に対し OSM の ``addr:housenumber`` は 180 件（1.0%）しかなく、外れたのか
OSM に無いのかを切り分けられない。

    uv run python scripts/eval_osm.py --city 鳥取市 --city 新宿区
    uv run python scripts/eval_osm.py --pref 鳥取県
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jev_abr_geocoder import Geocoder, GeocoderConfig, adapters  # noqa: E402
from jev_abr_geocoder.address import Point  # noqa: E402
from jev_abr_geocoder.index.keys import match_key  # noqa: E402
from jev_abr_geocoder.outcome import BatchOutcome  # noqa: E402

#: 公開の Overpass。混むと 504 を返すので順に当てる。
MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
USER_AGENT = "jev-abr-geocoder-eval/0.1 (+https://github.com/ujiuji1259/jev-abr-geocoder)"
SOURCE = "OpenStreetMap contributors (ODbL 1.0)"

#: 町字に相当する place の値。suburb は大字より広いことがあるので入れない。
PLACE_KINDS = ("quarter", "neighbourhood")

#: 市区町村の admin_level。政令指定都市の行政区は 8。
CITY_LEVELS = (7, 8)

#: 「大字」「字」はどちらのデータも付け方が違うので、比較鍵からは落とす。
_PREFIX_ANYWHERE = re.compile(r"大字|字")


def compare_key(text: str) -> str:
    return _PREFIX_ANYWHERE.sub("", match_key("", text))


@dataclass(frozen=True, slots=True)
class Case:
    pref: str
    city: str
    town: str
    point: Point
    #: True なら :attr:`town` に住所全体が入っている（番号レベルの評価）。
    composed: bool = False

    @property
    def query(self) -> str:
        return self.town if self.composed else f"{self.pref}{self.city}{self.town}"


def _overpass(query: str, cache: Path) -> dict[str, object]:
    """問い合わせて JSON を返す。取れたものはキャッシュして二度と叩かない。"""
    if cache.exists():
        return json.loads(cache.read_text())
    print(f"  Overpass に問い合わせ: {cache.name}")
    last: Exception | None = None
    for attempt, url in enumerate((*MIRRORS, *MIRRORS)):
        if attempt:
            time.sleep(5 * attempt)
        request = urllib.request.Request(  # noqa: S310 - 固定のホスト
            url,
            data=urllib.parse.urlencode({"data": query}).encode(),
            headers={"User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:  # noqa: S310
                payload = response.read()
        except Exception as exc:  # noqa: BLE001 - 公開サービスなので落ちても続ける
            print(f"    {url.split('/')[2]}: {exc}")
            last = exc
            continue
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(payload)
        return json.loads(payload)
    raise RuntimeError(f"Overpass から取得できなかった: {last}")


def cities_in(pref: str, cache_dir: Path) -> list[str]:
    """都道府県に属する市区町村の名前。"""
    levels = "|".join(str(level) for level in CITY_LEVELS)
    query = (
        f'[out:json][timeout:180];area["name"="{pref}"]["admin_level"="4"]->.p;'
        f'rel(area.p)["admin_level"~"^({levels})$"]["name"];out tags;'
    )
    payload = _overpass(query, cache_dir / f"osm-cities-{pref}.json")
    elements = payload.get("elements", [])
    names: list[str] = []
    for element in elements if isinstance(elements, list) else []:
        tags = element.get("tags", {}) if isinstance(element, dict) else {}
        name = tags.get("name")
        if isinstance(name, str):
            names.append(name)
    return sorted(set(names))


def cases_in(pref: str, city: str, cache_dir: Path) -> list[Case]:
    """市区町村の中の町字相当ノード。

    ``admin_level`` は正規表現にすると area の検索が重くなるので、値を順に当てる。
    """
    kinds = "|".join(PLACE_KINDS)
    cache = cache_dir / f"osm-{city}.json"
    payload: dict[str, object] = {}
    for level in CITY_LEVELS:
        query = (
            f'[out:json][timeout:180];area["name"="{city}"]["admin_level"="{level}"]->.a;'
            f'node(area.a)["place"~"^({kinds})$"]["name"];out tags center;'
        )
        payload = _overpass(query, cache)
        elements = payload.get("elements", [])
        if isinstance(elements, list) and elements:
            break
        cache.unlink(missing_ok=True)
    elements = payload.get("elements", [])
    out: list[Case] = []
    for element in elements if isinstance(elements, list) else []:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags", {})
        name, lat, lon = tags.get("name"), element.get("lat"), element.get("lon")
        if not isinstance(name, str) or lat is None or lon is None:
            continue
        out.append(
            Case(pref=pref, city=city, town=name, point=Point(lat=float(lat), lon=float(lon)))
        )
    return out


def number_cases_in(city: str, cache_dir: Path) -> list[Case]:
    """``addr:housenumber`` を持つ対象。番号レベルの評価に使う。

    日本の OSM は ``addr:province`` / ``addr:city`` / ``addr:quarter``（町字）/
    ``addr:block_number``（街区符号・地番の親番）/ ``addr:housenumber`` で住所を
    持つ。``addr:full`` があるときはそれを優先する — 人が書いた住所文字列その
    ままなので、入力としてはこちらが本物に近い。

    **被覆率は測れないが precision は測れる。** OSM にあるのは誰かが現地調査した
    場所だけで ABR の 1% 程度しかないので、ここから再現率は推定できない。ただし
    「OSM にある分について、こちらの答えが合っているか」は測れる。母集団が駅前や
    施設に偏っていることは解釈の際に引く。
    """
    cache = cache_dir / f"osm-numbers-{city}.json"
    payload: dict[str, object] = {}
    for level in CITY_LEVELS:
        query = (
            f'[out:json][timeout:180];area["name"="{city}"]["admin_level"="{level}"]->.a;'
            f'nwr(area.a)["addr:housenumber"];out tags center;'
        )
        payload = _overpass(query, cache)
        elements = payload.get("elements", [])
        if isinstance(elements, list) and elements:
            break
        cache.unlink(missing_ok=True)

    elements = payload.get("elements", [])
    out: list[Case] = []
    for element in elements if isinstance(elements, list) else []:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags", {})
        center = element.get("center", {})
        lat = element.get("lat", center.get("lat") if isinstance(center, dict) else None)
        lon = element.get("lon", center.get("lon") if isinstance(center, dict) else None)
        if lat is None or lon is None:
            continue
        query = _address_of(tags)
        if not query:
            continue
        out.append(
            Case(
                pref=tags.get("addr:province", ""),
                city=city,
                town=query,
                point=Point(lat=float(lat), lon=float(lon)),
                composed=True,
            )
        )
    return out


def _address_of(tags: dict[str, str]) -> str:
    """OSM のタグから住所文字列を組む。組めなければ空。"""
    full = tags.get("addr:full", "").strip()
    if full:
        return full
    pref, city = tags.get("addr:province", ""), tags.get("addr:city", "")
    town = tags.get("addr:quarter", "") + tags.get("addr:neighbourhood", "")
    if not (pref and city and town):
        # 町字が無いと番地だけになってしまう。評価に使えない。
        return ""
    block, house = tags.get("addr:block_number", ""), tags.get("addr:housenumber", "")
    number = f"{block}-{house}" if block else house
    return f"{pref}{city}{town}{number}"


def _unambiguous(cases: list[Case]) -> tuple[list[Case], int]:
    """同じ市区町村に同名のノードが複数あるものを外す。

    合併した市区町村では OSM が旧町名を付けずに「新町」とだけ持つことがあり、
    ``市区町村 + 新町`` という入力からはどの新町か決められない。**こちらの誤りでは
    なく評価セットの曖昧さ**なので、距離の母集団から外す（鳥取市では 20 km の
    外れ値がこれだった）。
    """
    counts = Counter((case.city, compare_key(case.town)) for case in cases)
    kept = [case for case in cases if counts[(case.city, compare_key(case.town))] == 1]
    return kept, len(cases) - len(kept)


def distance_m(a: Point, b: Point) -> float:
    radius = 6_371_000.0
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat, dlon = lat2 - lat1, math.radians(b.lon - a.lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    return sorted(values)[min(len(values) - 1, int(len(values) * q))]


#: 番号まで解決できた粒度。
_NUMBER_LEVELS = ("街区", "住居番号", "地番")


def _report_numbers(cases: list[Case], outcome: BatchOutcome, elapsed: float) -> None:
    """番号レベルの precision。距離は建物に打たれた点との隔たりなので数十 m を見る。"""
    levels: Counter[str] = Counter()
    distances: list[float] = []
    far: list[tuple[Case, str, float]] = []
    for case, result in zip(cases, outcome.results, strict=True):
        levels[result.granularity.label] += 1
        if result.granularity.label not in _NUMBER_LEVELS or result.point is None:
            continue
        d = distance_m(case.point, result.point)
        distances.append(d)
        if d > 200 and len(far) < 8:
            far.append((case, result.address, d))

    n = len(cases)
    reached = sum(levels[label] for label in _NUMBER_LEVELS)
    print(f"入力 {n:,} 件  所要 {elapsed:.1f} 秒  Jev 往復 {outcome.usage.requests}")
    print("\n粒度の分布")
    for label, count in levels.most_common():
        print(f"  {label:8} {count:>6,}  {count / n:.1%}")
    print(f"\n番号まで到達 {reached:,} / {n:,}  ({reached / n:.1%})")
    if distances:
        print("到達した分について、OSM が建物に打った点との隔たり [m]")
        for label, q in (("p50", 0.50), ("p90", 0.90), ("p99", 0.99)):
            print(f"  {label}  {_percentile(distances, q):>8,.0f}")
        print(f"  max  {max(distances):>8,.0f}")
        for threshold in (50, 200, 1000):
            within = sum(1 for d in distances if d <= threshold)
            print(f"  {threshold:>5} m 以内  {within:>6,}  {within / len(distances):.1%}")
    if far:
        print("\n200 m 以上離れた例")
        for case, address, d in far:
            print(f"  {d:>8,.0f} m  {case.query:32} -> {address}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--pref", help="この都道府県の市区町村を全部")
    parser.add_argument("--city", action="append", default=[], help="市区町村名。複数可")
    parser.add_argument(
        "--numbers",
        action="store_true",
        help="番号レベルを測る（addr:housenumber を持つ対象）。層2 を入れた索引が要る",
    )
    parser.add_argument("--model", action="store_true", help="Jev を使う")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()

    cache_dir = args.data_dir / "cache" / "eval"
    index = adapters.open_index(args.data_dir)
    pref_of = {c.city: c.pref for c in index.reader.cities()}
    index.close()

    targets: list[str] = list(args.city)
    if args.pref:
        targets.extend(cities_in(args.pref, cache_dir))
    if not targets:
        parser.error("--pref か --city のどちらかを指定してください")

    found: list[Case] = []
    for city in targets:
        if args.numbers:
            found.extend(number_cases_in(city, cache_dir))
            continue
        pref = args.pref or pref_of.get(city, "")
        if not pref:
            print(f"  {city}: 都道府県が引けないので飛ばす")
            continue
        found.extend(cases_in(pref, city, cache_dir))
    if args.numbers:
        cases, ambiguous = found, 0
        print(f"\n{SOURCE}: addr:housenumber を持つ対象 {len(found):,} 件")
        print("  被覆率は測れない（OSM にあるのは現地調査された場所だけ）。測るのは precision")
    else:
        cases, ambiguous = _unambiguous(found)
        print(f"\n{SOURCE}: 町字相当ノード {len(found):,} 件（{len(targets)} 市区町村）")
        print(f"  同名で曖昧なため除外 {ambiguous:,} 件")
    if not cases:
        return

    cfg = GeocoderConfig(batch_size=args.batch_size, concurrency=args.concurrency)
    model = adapters.jev_model(cfg) if args.model else None
    started = time.perf_counter()
    with Geocoder.open(args.data_dir, model=model, cfg=cfg) as geocoder:
        outcome = await geocoder.run_all([case.query for case in cases])
    elapsed = time.perf_counter() - started

    if args.numbers:
        _report_numbers(cases, outcome, elapsed)
        return

    matched: list[float] = []
    renamed: list[tuple[Case, str]] = []
    unreached = 0
    levels: Counter[str] = Counter()
    far: list[tuple[Case, str, float]] = []
    for case, result in zip(cases, outcome.results, strict=True):
        levels[result.granularity.label] += 1
        if not result.machiaza or result.point is None:
            unreached += 1
            continue
        ours, theirs = compare_key(result.machiaza), compare_key(case.town)
        if not (ours == theirs or theirs in ours or ours in theirs):
            # ABR に無い名前（旧町名・通称・OSM 側の誤り）。座標の評価には使えない。
            renamed.append((case, result.machiaza))
            continue
        d = distance_m(case.point, result.point)
        matched.append(d)
        if d > 1000 and len(far) < 10:
            far.append((case, result.address, d))

    n = len(cases)
    print(f"入力 {n:,} 件  所要 {elapsed:.1f} 秒  Jev 往復 {outcome.usage.requests}")
    print("\n粒度の分布")
    for label, count in levels.most_common():
        print(f"  {label:8} {count:>7,}  {count / n:.1%}")
    print("\n突き合わせ")
    print(f"  町字名が一致      {len(matched):>7,}  {len(matched) / n:.1%}  <- 距離を測るのはここ")
    print(
        f"  ABR に無い名前    {len(renamed):>7,}  {len(renamed) / n:.1%}"
        "  旧町名・通称・OSM 側の誤り"
    )
    print(f"  町字に届かない    {unreached:>7,}  {unreached / n:.1%}")
    if matched:
        print("\nOSM のノードとの隔たり [m]（数百 m は正常。見るのは 1 km 超）")
        for label, q in (("p50", 0.50), ("p90", 0.90), ("p99", 0.99)):
            print(f"  {label}  {_percentile(matched, q):>8,.0f}")
        print(f"  max  {max(matched):>8,.0f}")
        for threshold in (500, 1000, 5000):
            over = sum(1 for d in matched if d > threshold)
            print(f"  {threshold:>5} m 超  {over:>6,}  {over / len(matched):.1%}")
    if far:
        print("\n1 km 以上ずれた例")
        for case, address, d in far:
            print(f"  {d:>8,.0f} m  {case.query:30} -> {address}")
    if renamed:
        print("\nABR に無い名前の例")
        for case, ours in renamed[:5]:
            print(f"  {case.query:30} -> {ours}")


if __name__ == "__main__":
    asyncio.run(main())
