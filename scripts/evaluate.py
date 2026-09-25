#!/usr/bin/env python
"""評価ハーネス。

docs/eval.md の指標を測る。パッケージには含めない開発用ツール。

    # Jev なし（トライだけ）の素性を見る
    uv run python scripts/evaluate.py --data-dir data

    # Jev あり。閾値を振って精度-カバレッジ曲線を出す
    TYPESAFE_API_KEY=$(cat ~/.config/typesafe/key) \\
        uv run python scripts/evaluate.py --data-dir data --model --sweep

**測るべきは 2 つを分けた数字**（docs/eval.md §7）:

- **候補再現率** … 正解が Jev に渡す前の候補集合に入っていた割合。トライの責任
- **選択精度**   … その中で Jev が正解を選べた割合。Jev の責任

分けないと、精度が出ないときにトライを直すのか質問文を直すのか判断できない。
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import csv
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jev_abr_geocoder.config import GeocoderConfig  # noqa: E402
from jev_abr_geocoder.geocoder import Geocoder  # noqa: E402
from jev_abr_geocoder.match.rerank import DecisionModel, JevModel  # noqa: E402
from jev_abr_geocoder.models import GeocodeResult  # noqa: E402
from jev_abr_geocoder.textnorm import normalize  # noqa: E402

#: Jev の入力トークン単価（出力は無料）。https://typesafe.ai/blog/...
INPUT_COST_PER_MTOK = 0.042

csv.field_size_limit(10_000_000)


# ------------------------------------------------------------------ 計測用


class CountingModel:
    """Jev 呼び出しの回数とトークンを数えるラッパ。"""

    def __init__(self, inner: DecisionModel) -> None:
        self._inner = inner
        self.requests = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.latencies: list[float] = []

    async def ask(self, state: object, questions: object) -> object:
        started = time.perf_counter()
        answers, tokens = await self._inner.ask(state, questions)  # type: ignore[arg-type]
        self.latencies.append((time.perf_counter() - started) * 1000)
        self.requests += 1
        self.input_tokens += tokens[0]
        self.output_tokens += tokens[1]
        return answers, tokens


@dataclass
class Report:
    total: int = 0
    elapsed: float = 0.0
    levels: collections.Counter[str] = field(default_factory=collections.Counter)
    resolved: int = 0
    town_fast_path: int = 0
    number_fast_path: int = 0
    requests: int = 0
    input_tokens: int = 0
    latencies: list[float] = field(default_factory=list)
    #: 銀ラベルがある場合の突き合わせ
    silver_total: int = 0
    silver_recall: int = 0
    silver_correct: int = 0
    disagreements: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def cost(self) -> float:
        return self.input_tokens / 1_000_000 * INPUT_COST_PER_MTOK


# ------------------------------------------------------------------ 銀ラベル


#: 丁目の漢数字。比較のときだけ算用数字に寄せる。
_KANJI_CHOME = re.compile(r"([〇一二三四五六七八九十百]+)(丁目|丁)")
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
    return total + current


def comparable(address: str) -> str:
    """突き合わせ専用の正規形。

    geolonia は丁目を漢数字に正規化して返す (「奈佐原二丁目」) が、こちらは
    ABR の収録表記をそのまま返す (「奈佐原2丁目」— ABR 側が全角算用数字)。
    **どちらも正しい住所**なので、比較のときだけ表記を寄せる。

    これをやらないと一致率が 32% と出て、実装の誤りに見えてしまう。
    """
    folded = _KANJI_CHOME.sub(
        lambda m: f"{_kanji_to_int(m.group(1))}{m.group(2)}", normalize(address)
    )
    return folded.replace("大字", "").replace("字", "")


def load_silver(path: Path) -> dict[str, str]:
    """geolonia の出力を銀ラベルとして読む。

    **これは正解データではない。** geolonia 自身の実装を走らせた
    スナップショットなので、一致率は「geolonia との合意率」でしかない
    （docs/eval.md §4）。不一致集合を人手で見るための道具として使う。
    """
    out: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[row["住所"]] = comparable(row["都道府県"] + row["市区町村"] + row["町字"])
    return out


def _predicted_town(result: GeocodeResult) -> str:
    return comparable(result.pref + result.county + result.city + result.ward + result.town)


# ------------------------------------------------------------------ 実行


async def run(
    data_dir: Path,
    queries: list[str],
    cfg: GeocoderConfig,
    *,
    use_model: bool,
    silver: dict[str, str] | None,
) -> Report:
    model: DecisionModel | None = None
    counter: CountingModel | None = None
    if use_model:
        counter = CountingModel(JevModel.from_env(cfg))
        model = counter  # type: ignore[assignment]

    report = Report(total=len(queries))
    started = time.perf_counter()
    with Geocoder.open(data_dir, model=model, cfg=cfg) as geocoder:
        for start in range(0, len(queries), cfg.batch_size):
            batch = queries[start : start + cfg.batch_size]
            outcome = await geocoder.run(batch)
            report.town_fast_path += outcome.town_fast_path
            report.number_fast_path += outcome.number_fast_path
            for query, result in zip(batch, outcome.results, strict=True):
                report.levels[result.level.label] += 1
                report.resolved += bool(result.resolved)
                if silver is not None and query in silver:
                    expected = silver[query]
                    if not expected:
                        continue
                    report.silver_total += 1
                    got = _predicted_town(result)
                    if got == expected:
                        report.silver_correct += 1
                    elif len(report.disagreements) < 40:
                        report.disagreements.append((query, expected, got or "(なし)"))
    report.elapsed = time.perf_counter() - started
    if counter is not None:
        report.requests = counter.requests
        report.input_tokens = counter.input_tokens
        report.latencies = counter.latencies
    return report


def print_report(report: Report, title: str) -> None:
    print(f"\n===== {title} =====")
    print(f"入力            {report.total:,} 件")
    per_item = report.elapsed / report.total * 1000
    print(f"所要            {report.elapsed:.2f} 秒  ({per_item:.2f} ms/件)")
    print("\n粒度の分布:")
    for level, count in sorted(report.levels.items(), key=lambda kv: -kv[1]):
        print(f"  {level:8} {count:7,}  {count / report.total:6.1%}")
    print(f"\nresolved=True   {report.resolved:,} ({report.resolved / report.total:.1%})")
    print(f"ファストパス    町字 {report.town_fast_path:,} / 番号 {report.number_fast_path:,}")
    if report.requests:
        lat = sorted(report.latencies)
        print(f"\nJev 往復        {report.requests:,} 回")
        print(f"入力トークン    {report.input_tokens:,}")
        per_k = report.cost / report.total * 1000
        print(f"概算コスト      ${report.cost:.4f}  (${per_k:.4f} / 1,000 件)")
        if lat:
            print(
                f"1 往復の時間    p50 {lat[len(lat) // 2]:.0f}ms  "
                f"p95 {lat[int(len(lat) * 0.95)]:.0f}ms  max {lat[-1]:.0f}ms"
            )
    if report.silver_total:
        rate = report.silver_correct / report.silver_total
        print(
            f"\n銀ラベルとの一致 {report.silver_correct:,} / {report.silver_total:,} ({rate:.2%})"
        )
        print("  ※ geolonia 自身の出力との合意率であって正解率ではない")
        if report.disagreements:
            print("\n不一致（人手で見るべき集合）:")
            for query, expected, got in report.disagreements[:20]:
                print(f"    {query!r:40}\n        geolonia={expected!r}\n        こちら  ={got!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("tests/data/geolonia/list.txt"),
        help="1 行 1 住所。カンマ以降は備考として無視する",
    )
    parser.add_argument("--silver", type=Path, help="geolonia の addresses.csv")
    parser.add_argument("--limit", type=int, help="先頭 N 件だけ")
    parser.add_argument("--model", action="store_true", help="Jev を使う")
    parser.add_argument("--always-rerank", action="store_true", help="ファストパスを無効化")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument(
        "--sweep", action="store_true", help="confidence 閾値を振って精度-カバレッジを出す"
    )
    parser.add_argument("--jsonl", type=Path, help="結果を JSON Lines で書き出す")
    args = parser.parse_args()

    if not args.input.exists():
        parser.error(f"入力が無い: {args.input}")
    queries = [
        line.split(",")[0].strip()
        for line in args.input.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.limit:
        queries = queries[: args.limit]
    silver = load_silver(args.silver) if args.silver else None

    if not args.sweep:
        cfg = GeocoderConfig(batch_size=args.batch_size, always_rerank=args.always_rerank)
        report = asyncio.run(run(args.data_dir, queries, cfg, use_model=args.model, silver=silver))
        print_report(report, "Jev あり" if args.model else "Jev なし（トライのみ）")
        if args.jsonl:
            args.jsonl.write_text(
                json.dumps(
                    {
                        "total": report.total,
                        "levels": dict(report.levels),
                        "resolved": report.resolved,
                        "requests": report.requests,
                        "cost": report.cost,
                    },
                    ensure_ascii=False,
                )
            )
        return

    # 精度-カバレッジ曲線。閾値はここから動作点として選ぶ（docs/eval.md §8）。
    print("\n閾値スイープ（町字の confidence）")
    print(f"{'閾値':>6} {'カバレッジ':>10} {'銀ラベル一致':>12} {'Jev 往復':>9} {'コスト':>10}")
    print("-" * 52)
    for threshold in (0.0, 0.3, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95):
        cfg = GeocoderConfig(
            batch_size=args.batch_size,
            always_rerank=args.always_rerank,
            town_confidence=threshold,
            number_confidence=threshold,
        )
        report = asyncio.run(run(args.data_dir, queries, cfg, use_model=args.model, silver=silver))
        coverage = report.resolved / report.total
        agree = report.silver_correct / report.silver_total if report.silver_total else 0.0
        print(
            f"{threshold:>6.2f} {coverage:>10.1%} {agree:>12.2%} "
            f"{report.requests:>9,} {f'${report.cost:.4f}':>10}"
        )


if __name__ == "__main__":
    main()
