"""郵便番号データとの突き合わせ（docs/eval.md §12）。

日本郵便の郵便番号データ（`utf_ken_all.zip`、約12万件）を入力に流す。ABR とは
**別系統で整備された住所表記**なので、ABR の正規表記を流す合成データでは出ない
ずれが出る。

ラベル:

- **市区町村は金ラベル** — 1 列目の全国地方公共団体コード（JIS X 0402 の 5 桁）に
  検査数字を付ければ ABR の ``lg_code`` に決定的に変換できる。手元の 1,918 市区町村
  で検算済み
- **町域名は準金ラベル** — ABR の町字と粒度が近いが、丁目を括弧で持つ・「以下に
  掲載がない場合」のような特殊行がある・大字と小字の切り方が違う、ので文字列一致で
  しか見られない

データはコミットしない（利用条件を配布ページから読み取れなかったため）。取得は
このスクリプトが行い、``data/cache/eval/`` に置く（``data/`` は .gitignore 済み）。

    uv run python scripts/eval_zipcode.py --limit 3000
    uv run python scripts/eval_zipcode.py --limit 2000 --model --batch-size 16
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import random
import re
import sys
import time
import urllib.request
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jev_abr_geocoder import Geocoder, GeocoderConfig, adapters  # noqa: E402
from jev_abr_geocoder.index.keys import match_key  # noqa: E402

DATA_URL = "https://www.post.japanpost.jp/service/search/zipcode/download/utf/zip/utf_ken_all.zip"
SOURCE = "郵便番号データ（日本郵便株式会社）"

#: 町域名に入る、住所ではない断り書き。この行は評価に使えない。
_NOT_A_TOWN = (
    "以下に掲載がない場合",
    "の次に番地がくる場合",
    "地階・階層不明",
    "（",  # 「（１丁目）」「（次のビルを除く）」など。括弧付きは扱いが分かれるので外す
)


#: 「大字」「字」は構造の印で名前の一部ではない。ABR は ``歌津字田茂川`` のように
#: **途中に**挟むが、郵便番号データは ``歌津田茂川`` と持つ。段の切り方が両者で
#: 違うので、:func:`keys.match_key` のように段ごとに落とすことができない。
#: 比較鍵としては**どこにあっても落とす**。両側に同じ処理をかけるので、
#: 「大文字町」が「大文町」になっても比較としては成り立つ。
_PREFIX_ANYWHERE = re.compile(r"大字|字")


def compare_key(text: str) -> str:
    """町字名を突き合わせるための鍵。漢数字とケ/ヶ の揺れは match_key が吸収する。"""
    return _PREFIX_ANYWHERE.sub("", match_key("", text))


@dataclass(frozen=True, slots=True)
class Case:
    """1 行。``lg_code`` が金ラベル、``town`` が準金ラベル。"""

    lg_code: int
    zipcode: str
    pref: str
    city: str
    town: str

    @property
    def query(self) -> str:
        return f"{self.pref}{self.city}{self.town}"


def check_digit(jis5: int) -> int:
    """全国地方公共団体コードの検査数字。重みは左から 6,5,4,3,2。

    >>> check_digit(31201), check_digit(13110), check_digit(42391)
    (1, 5, 2)
    """
    total = sum(int(d) * w for d, w in zip(f"{jis5:05d}", (6, 5, 4, 3, 2), strict=True))
    rest = 11 - (total % 11)
    return {10: 0, 11: 1}.get(rest, rest)


def download(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "utf_ken_all.zip"
    if path.exists():
        return path
    print(f"取得中: {DATA_URL}")
    with urllib.request.urlopen(DATA_URL) as response:  # noqa: S310 - 固定の URL
        tmp = path.with_suffix(".part")
        tmp.write_bytes(response.read())
        tmp.replace(path)
    return path


def read_cases(path: Path) -> list[Case]:
    """zip 内の CSV を読む。列は仕様書どおりの位置で取る。"""
    out: list[Case] = []
    with zipfile.ZipFile(path) as archive:
        name = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
        with archive.open(name) as raw:
            stream = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            for row in csv.reader(stream):
                if len(row) < 9:
                    continue
                town = row[8].strip()
                if not town or any(mark in town for mark in _NOT_A_TOWN):
                    continue
                jis5 = int(row[0])
                out.append(
                    Case(
                        lg_code=jis5 * 10 + check_digit(jis5),
                        zipcode=row[2].strip(),
                        pref=row[6].strip(),
                        city=row[7].strip(),
                        town=town,
                    )
                )
    return out


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--limit", type=int, default=3000, help="無作為に N 件")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--model", action="store_true", help="Jev を使う")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()

    cases = read_cases(download(args.data_dir / "cache" / "eval"))
    print(f"{SOURCE}: 評価に使える行 {len(cases):,} 件")
    if args.limit and args.limit < len(cases):
        cases = random.Random(args.seed).sample(cases, args.limit)

    cfg = GeocoderConfig(batch_size=args.batch_size, concurrency=args.concurrency)
    model = adapters.jev_model(cfg) if args.model else None
    started = time.perf_counter()
    with Geocoder.open(args.data_dir, model=model, cfg=cfg) as geocoder:
        outcome = await geocoder.run_all([case.query for case in cases])
    elapsed = time.perf_counter() - started

    city_ok = town_reached = town_match = 0
    levels: Counter[str] = Counter()
    misses: list[tuple[Case, str]] = []
    for case, result in zip(cases, outcome.results, strict=True):
        levels[result.granularity.label] += 1
        if result.lg_code == f"{case.lg_code:06d}":
            city_ok += 1
        elif len(misses) < 5:
            misses.append((case, f"市区町村ちがい {result.lg_code or '-'} ({result.address})"))
        if result.machiaza:
            town_reached += 1
            ours, theirs = compare_key(result.machiaza), compare_key(case.town)
            if ours == theirs or theirs in ours or ours in theirs:
                town_match += 1
            elif len(misses) < 10:
                misses.append((case, f"町字ちがい {result.machiaza}  ({ours} != {theirs})"))

    n = len(cases)
    print(f"\n入力 {n:,} 件  所要 {elapsed:.1f} 秒 ({elapsed / n * 1000:.2f} ms/件)")
    print(f"Jev 往復 {outcome.usage.requests}  入力トークン {outcome.usage.input_tokens:,}")
    print("\n市区町村（金ラベル）")
    print(f"  正解率        {city_ok:>7,} / {n:,}  {city_ok / n:.2%}")
    print("\n町字（準金ラベル）")
    print(f"  町字まで到達  {town_reached:>7,}  {town_reached / n:.1%}")
    print(
        f"  町域名と一致  {town_match:>7,}  {town_match / n:.1%}"
        f"  (到達分のうち {town_match / town_reached:.1%})"
        if town_reached
        else ""
    )
    print("\n粒度の分布")
    for label, count in levels.most_common():
        print(f"  {label:8} {count:>7,}  {count / n:.1%}")
    if misses:
        print("\n外れた例")
        for case, why in misses[:10]:
            print(f"  {case.query:34} -> {why}")


if __name__ == "__main__":
    asyncio.run(main())
