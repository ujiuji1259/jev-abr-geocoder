#!/usr/bin/env python
"""法人番号公表データでの評価。

docs/eval.md の Tier 1'。**人間が入力した生の住所**に対して、市区町村だけは
金ラベルが手に入るのが利点。

    uv run python scripts/evaluate_houjin.py --data-dir data/tottori \\
        --csv .../31_tottori_all_20260831.csv

国税庁のリソース定義書で確認した列（1 始まり）:

===  ======================  ==================================================
列    項目                    評価での役割
===  ======================  ==================================================
16   国内所在地（都道府県）      入力の組み立て
17   国内所在地（市区町村）      入力の組み立て
18   国内所在地（丁目番地等）    **入力**。建物名・部屋番号を含む生の表記
20   都道府県コード             JIS X 0401
21   市区町村コード             JIS X 0402。20 と合わせて **市区町村の金ラベル**
22   郵便番号                  国税庁が住所文字列から導出したもの。弱いラベル
===  ======================  ==================================================

JIS 5 桁から ABR の lg_code (6 桁) へはチェックディジットで決定的に変換できる。
鳥取市 31201 -> 312011、岩美町 31302 -> 313025 などで検算済み。
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import csv
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jev_abr_geocoder import adapters, ports  # noqa: E402
from jev_abr_geocoder.address import Granularity  # noqa: E402
from jev_abr_geocoder.config import GeocoderConfig  # noqa: E402
from jev_abr_geocoder.geocoder import Geocoder  # noqa: E402
from jev_abr_geocoder.outcome import GeocodeResult  # noqa: E402

INPUT_COST_PER_MTOK = 0.042

csv.field_size_limit(10_000_000)

# 0 始まりの列位置。
_PREF = 9
_CITY = 10
_STREET = 11
_PREF_CODE = 13
_CITY_CODE = 14
_POSTCODE = 15


def check_digit(jis5: str) -> str:
    """全国地方公共団体コードの検査数字。

    重み 6,5,4,3,2 を掛けた和を 11 で割り、11 から余りを引いた数の下 1 桁。
    """
    weights = (6, 5, 4, 3, 2)
    total = sum(int(d) * w for d, w in zip(jis5, weights, strict=True))
    return str((11 - total % 11) % 10)


@dataclass(frozen=True, slots=True)
class Case:
    query: str
    #: JIS 5 桁から導いた ABR の lg_code（6 桁）。金ラベル。
    lg_code: str
    postcode: str


def load_cases(path: Path, limit: int | None) -> list[Case]:
    out: list[Case] = []
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.reader(f):
            if len(row) <= _POSTCODE:
                continue
            pref, city, street = row[_PREF], row[_CITY], row[_STREET]
            if not (pref and city and street):
                continue
            pref_code, city_code = row[_PREF_CODE], row[_CITY_CODE]
            if not (pref_code.isdigit() and city_code.isdigit()):
                continue
            jis5 = f"{int(pref_code):02d}{int(city_code):03d}"
            out.append(
                Case(
                    query=pref + city + street,
                    lg_code=jis5 + check_digit(jis5),
                    postcode=row[_POSTCODE],
                )
            )
            if limit and len(out) >= limit:
                break
    return out


class CountingModel:
    """:class:`ports.DecisionModel` を満たす、回数とトークンを数えるラッパ。"""

    def __init__(self, inner: ports.DecisionModel) -> None:
        self._inner = inner
        self.requests = 0
        self.input_tokens = 0
        self.latencies: list[float] = []

    async def choose(self, questions: Sequence[ports.Question]) -> ports.Answers:
        started = time.perf_counter()
        result = await self._inner.choose(questions)
        self.latencies.append((time.perf_counter() - started) * 1000)
        self.requests += 1
        self.input_tokens += result.usage.input_tokens
        return result


@dataclass
class Report:
    total: int = 0
    elapsed: float = 0.0
    levels: collections.Counter[str] = field(default_factory=collections.Counter)
    resolved: int = 0
    machiaza_fast_path: int = 0
    banchi_fast_path: int = 0
    narrow_requests: int = 0
    requests: int = 0
    input_tokens: int = 0
    latencies: list[float] = field(default_factory=list)
    #: 市区町村の金ラベルとの突き合わせ
    city_checked: int = 0
    city_correct: int = 0
    city_wrong: list[tuple[str, str, str]] = field(default_factory=list)
    #: 町字で止まった理由（note 別）
    stopped: collections.Counter[str] = field(default_factory=collections.Counter)
    stopped_examples: dict[str, list[str]] = field(default_factory=dict)
    #: 番号まで到達したものの確信度分布
    number_conf: list[float] = field(default_factory=list)
    town_conf: list[float] = field(default_factory=list)

    @property
    def cost(self) -> float:
        return self.input_tokens / 1_000_000 * INPUT_COST_PER_MTOK


async def run(data_dir: Path, cases: list[Case], cfg: GeocoderConfig, *, use_model: bool) -> Report:
    counter: CountingModel | None = None
    model: ports.DecisionModel | None = None
    if use_model:
        counter = CountingModel(adapters.jev_model(cfg))
        model = counter  # type: ignore[assignment]

    report = Report(total=len(cases))
    started = time.perf_counter()
    with Geocoder.open(data_dir, model=model, cfg=cfg) as geocoder:
        outcome = await geocoder.run_all([c.query for c in cases])
        report.machiaza_fast_path = outcome.machiaza_fast_path
        report.banchi_fast_path = outcome.banchi_fast_path
        report.narrow_requests = outcome.narrow_requests
        for case, result in zip(cases, outcome.results, strict=True):
            _record(report, case, result)
    report.elapsed = time.perf_counter() - started
    if counter is not None:
        report.requests = counter.requests
        report.input_tokens = counter.input_tokens
        report.latencies = counter.latencies
    return report


def _record(report: Report, case: Case, result: GeocodeResult) -> None:
    report.levels[result.granularity.label] += 1
    report.resolved += bool(result.resolved)
    if result.granularity >= Granularity.CITY and result.lg_code:
        report.city_checked += 1
        if result.lg_code == case.lg_code:
            report.city_correct += 1
        elif len(report.city_wrong) < 30:
            report.city_wrong.append((case.query, case.lg_code, result.lg_code))
    if result.granularity >= Granularity.BLOCK:
        report.number_conf.append(result.confidence)
    elif result.granularity == Granularity.MACHIAZA:
        report.town_conf.append(result.confidence)
        why = result.note or ("番地が入力に無い" if not result.remainder else "番号を選べなかった")
        report.stopped[why] += 1
        report.stopped_examples.setdefault(why, [])
        if len(report.stopped_examples[why]) < 5:
            report.stopped_examples[why].append(f"{result.query}  -> 残り {result.remainder!r}")


def _percentiles(values: list[float]) -> str:
    if not values:
        return "(なし)"
    ordered = sorted(values)
    n = len(ordered)

    def at(p: float) -> float:
        return ordered[min(int(n * p), n - 1)]

    return f"p10 {at(0.10):.2f}  p50 {at(0.50):.2f}  p90 {at(0.90):.2f}"


def print_report(report: Report, title: str) -> None:
    print(f"\n===== {title} =====")
    print(f"入力              {report.total:,} 件")
    per = report.elapsed / report.total * 1000 if report.total else 0
    print(f"所要              {report.elapsed:.1f} 秒  ({per:.2f} ms/件)")

    print("\n粒度の分布:")
    for level in ("地番", "住居番号", "街区", "町字", "市区町村", "都道府県", "不明"):
        count = report.levels.get(level, 0)
        if count:
            print(f"  {level:8} {count:8,}  {count / report.total:6.2%}")
    deep = sum(report.levels.get(x, 0) for x in ("地番", "住居番号", "街区"))
    print(f"\n番号まで到達      {deep:,} ({deep / report.total:.2%})")
    print(f"resolved=True     {report.resolved:,} ({report.resolved / report.total:.2%})")

    if report.city_checked:
        rate = report.city_correct / report.city_checked
        print(f"\n市区町村の正解率  {report.city_correct:,} / {report.city_checked:,} ({rate:.3%})")
        print("  ※ 法人番号の市区町村コード由来の金ラベル")
        if report.city_wrong:
            print("  誤り:")
            for query, want, got in report.city_wrong[:10]:
                print(f"    {query!r:44} 正 {want} / 出 {got}")

    if report.stopped:
        print("\n町字で止まった理由:")
        for why, count in report.stopped.most_common(6):
            print(f"  {count:6,}  {why}")
            for ex in report.stopped_examples.get(why, [])[:3]:
                print(f"           {ex}")

    print("\n確信度の分布:")
    print(f"  番号まで到達      {_percentiles(report.number_conf)}")
    print(f"  町字で止まった    {_percentiles(report.town_conf)}")

    print(
        f"\nファストパス      町字 {report.machiaza_fast_path:,} / 番号 {report.banchi_fast_path:,}"
    )
    if report.requests:
        lat = sorted(report.latencies)
        print(f"Jev 往復          {report.requests:,} 回 (うち絞り込み {report.narrow_requests:,})")
        print(f"入力トークン      {report.input_tokens:,}")
        per_k = report.cost / report.total * 1000
        print(f"概算コスト        ${report.cost:.4f}  (${per_k:.4f} / 1,000 件)")
        if lat:
            print(
                f"1 往復の時間      p50 {lat[len(lat) // 2]:.0f}ms  "
                f"p95 {lat[int(len(lat) * 0.95)]:.0f}ms"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True, help="法人番号の全件データ CSV")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--model", action="store_true", help="Jev を使う")
    parser.add_argument(
        "--always-rerank",
        action="store_true",
        help="ファストパスを無効化して全件 Jev を通す（閾値を測るとき用）",
    )
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--jsonl", type=Path)
    args = parser.parse_args()

    cases = load_cases(args.csv, args.limit)
    if not cases:
        parser.error(f"読み込めなかった: {args.csv}")
    cfg = GeocoderConfig(
        batch_size=args.batch_size,
        concurrency=args.concurrency,
        always_ask=args.always_ask,
    )
    report = asyncio.run(run(args.data_dir, cases, cfg, use_model=args.model))
    print_report(report, "Jev あり" if args.model else "Jev なし（トライのみ）")
    if args.jsonl:
        args.jsonl.write_text(
            json.dumps(
                {
                    "total": report.total,
                    "levels": dict(report.levels),
                    "city_correct": report.city_correct,
                    "city_checked": report.city_checked,
                    "requests": report.requests,
                    "cost": report.cost,
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
