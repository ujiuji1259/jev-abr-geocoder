"""摂動注入 — 誤字・異体字への耐性を曲線で出す（docs/eval.md §11）。

ABR の正規表記から入力を作り、**機械的に崩してから**投げる。正解は崩す前の町字
なのでラベルコストは 0 で、難易度を任意に上げられる。

測るのは 2 つ（docs/eval.md §7）:

- **候補再現率** … 正解が Jev に渡す前の候補集合に入っていたか。トライの責任
- **町字正解率** … 最終的に正解の町字を返せたか。Jev まで通した結果

この 2 つを分けて条件ごとに並べると、崩し方によってどちらが効かなくなるのかが
見える。異体字は Jev の売りなので、そこで正解率が落ちないことが確認したいこと。

    uv run python scripts/eval_perturb.py --limit 300
    uv run python scripts/eval_perturb.py --limit 200 --model --batch-size 16
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jev_abr_geocoder import Geocoder, GeocoderConfig, adapters  # noqa: E402
from jev_abr_geocoder.address import MachiazaRecord  # noqa: E402
from jev_abr_geocoder.match.candidates import CandidateFinder  # noqa: E402
from jev_abr_geocoder.textnorm import normalize  # noqa: E402

#: 実在する異体字のゆらぎ。ABR は左、入力には右が来ることがある。
#: 索引側のエイリアスでは吸収していないので、ここは判定モデルの担当になる。
VARIANTS = {
    "ケ": "ヶ",
    "ヶ": "ケ",
    "崎": "﨑",
    "浜": "濵",
    "館": "舘",
    "曽": "曾",
    "沢": "澤",
    "斎": "齋",
    "竜": "龍",
    "辺": "邊",
    "島": "嶋",
    "藤": "藤",
    "桧": "檜",
    "槻": "槻",
}

#: 視覚的に似ていて実際に打ち間違える字。
CONFUSABLE = {
    "田": "由",
    "本": "木",
    "町": "丁",
    "川": "州",
    "谷": "答",
    "野": "埜",
    "原": "厚",
    "松": "枩",
    "中": "申",
    "大": "犬",
    "上": "止",
    "下": "不",
}

_ZEN = str.maketrans("0123456789-", "０１２３４５６７８９－")
_KANJI = "〇一二三四五六七八九"


@dataclass(frozen=True, slots=True)
class Case:
    """1 件の評価入力。``row_id`` が正解。"""

    row_id: int
    query: str


#: 入力の末尾に付ける番地。実際の住所の形にしておかないと、全角化のような
#: 数字に効く崩しが試せない。
_TAIL = "1-2"


def _canonical(record: MachiazaRecord) -> str:
    return f"{record.name.display}{_TAIL}"


def _machiaza_span(record: MachiazaRecord, query: str) -> tuple[int, int]:
    """町字の部分の範囲。崩すのはここだけにする（番地は崩さない）。"""
    start = len(record.name.city_text)
    return start, len(query) - len(_TAIL)


# ------------------------------------------------------------------ 崩し方


def _as_is(record: MachiazaRecord, query: str, rng: random.Random) -> str | None:
    return query


def _zenkaku(record: MachiazaRecord, query: str, rng: random.Random) -> str | None:
    out = query.translate(_ZEN)
    return out if out != query else None


def _arabic_chome(record: MachiazaRecord, query: str, rng: random.Random) -> str | None:
    """「一丁目」を「1丁目」にする。索引側のエイリアスが吸収するはずの崩し。"""
    chome = record.name.chome
    if not chome or not chome.endswith("丁目"):
        return None
    number = "".join(str(_KANJI.index(c)) for c in chome[:-2] if c in _KANJI)
    if not number:
        return None
    return query.replace(chome, f"{number}丁目")


def _drop_pref(record: MachiazaRecord, query: str, rng: random.Random) -> str | None:
    pref = record.name.pref
    return query[len(pref) :] if pref and query.startswith(pref) else None


def _variant(record: MachiazaRecord, query: str, rng: random.Random) -> str | None:
    """異体字に 1 文字だけ置き換える。"""
    start, end = _machiaza_span(record, query)
    spots = [i for i in range(start, end) if query[i] in VARIANTS]
    if not spots:
        return None
    i = rng.choice(spots)
    return query[:i] + VARIANTS[query[i]] + query[i + 1 :]


def _typo(record: MachiazaRecord, query: str, rng: random.Random) -> str | None:
    """似た字に 1 文字だけ打ち間違える。"""
    start, end = _machiaza_span(record, query)
    spots = [i for i in range(start, end) if query[i] in CONFUSABLE]
    if not spots:
        return None
    i = rng.choice(spots)
    return query[:i] + CONFUSABLE[query[i]] + query[i + 1 :]


def _drop_char(record: MachiazaRecord, query: str, rng: random.Random) -> str | None:
    """町字の 1 文字を落とす。"""
    start, end = _machiaza_span(record, query)
    spots = [i for i in range(start, end) if not query[i].isdigit()]
    if len(spots) < 2:
        return None
    i = rng.choice(spots)
    return query[:i] + query[i + 1 :]


Perturbation = Callable[[MachiazaRecord, str, random.Random], "str | None"]

CONDITIONS: list[tuple[str, Perturbation]] = [
    ("崩さない", _as_is),
    ("全角", _zenkaku),
    ("丁目を算用数字", _arabic_chome),
    ("都道府県を落とす", _drop_pref),
    ("異体字 1 文字", _variant),
    ("誤字 1 文字", _typo),
    ("1 文字脱落", _drop_char),
]


@dataclass(frozen=True, slots=True)
class Score:
    condition: str
    cases: int
    recall: float
    correct: float
    resolved: float
    fast_path: float
    requests: int


async def _score(
    condition: str,
    cases: list[Case],
    data_dir: Path,
    cfg: GeocoderConfig,
    *,
    use_model: bool,
) -> Score:
    index = adapters.open_index(data_dir)
    finder = CandidateFinder(index, cfg)
    in_candidates = sum(
        1
        for case in cases
        if case.row_id in {c.row_id for c in finder.find(normalize(case.query)).candidates}
    )
    index.close()

    model = adapters.jev_model(cfg) if use_model else None
    with Geocoder.open(data_dir, model=model, cfg=cfg) as geocoder:
        outcome = await geocoder.run_all([case.query for case in cases])

    correct = 0
    for case, result in zip(cases, outcome.results, strict=True):
        if result.machiaza_id and result.machiaza_id == _expected(case, data_dir):
            correct += 1
    return Score(
        condition=condition,
        cases=len(cases),
        recall=in_candidates / len(cases),
        correct=correct / len(cases),
        resolved=sum(1 for r in outcome.results if r.resolved) / len(cases),
        fast_path=outcome.machiaza_fast_path / len(cases),
        requests=outcome.usage.requests,
    )


_EXPECTED: dict[tuple[Path, int], str] = {}


def _expected(case: Case, data_dir: Path) -> str:
    return _EXPECTED[(data_dir, case.row_id)]


def _build_cases(data_dir: Path, limit: int, seed: int) -> dict[str, list[Case]]:
    """条件ごとの入力を作る。どの条件にも当てはまる町字だけを使う。"""
    reader = adapters.open_reader(data_dir)
    total = reader.machiaza_count()
    rng = random.Random(seed)
    picked: list[MachiazaRecord] = []
    tried: set[int] = set()
    while len(picked) < limit and len(tried) < total:
        row_id = rng.randrange(total)
        if row_id in tried:
            continue
        tried.add(row_id)
        record = reader.machiaza([row_id]).get(row_id)
        if record is None or not record.from_abr or not record.name.oaza_cho:
            continue
        # 小字が数字そのものの町字（「鯖江市上河内町１２」）は外す。番地を足すと
        # 「上河内町１２1-2」になり、町字の数字なのか番地なのか入力から決められない。
        # 索引は「数字列の途中で切る一致は成立しない」という規則で親の町字に落とす
        # ので正しい挙動だが、**評価のラベルが付けられない入力**なので使わない。
        if normalize(record.name.display)[-1].isdigit():
            continue
        picked.append(record)
        _EXPECTED[(data_dir, row_id)] = record.machiaza_code
    reader.close()

    out: dict[str, list[Case]] = {name: [] for name, _ in CONDITIONS}
    for record in picked:
        base = _canonical(record)
        for name, perturb in CONDITIONS:
            query = perturb(record, base, rng)
            if query and query != record.name.city_text:
                out[name].append(Case(row_id=record.row_id, query=query))
    return out


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--limit", type=int, default=300, help="町字のサンプル数")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--model", action="store_true", help="Jev を使う")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()

    cfg = GeocoderConfig(batch_size=args.batch_size, concurrency=args.concurrency)
    cases = _build_cases(args.data_dir, args.limit, args.seed)

    print(f"町字 {args.limit} 件を崩して投げる（Jev {'あり' if args.model else 'なし'}）\n")
    header = (
        f"{'崩し方':18} {'件数':>5} {'候補再現率':>9} {'町字正解率':>9}"
        f" {'resolved':>9} {'ファスト':>8} {'往復':>5}"
    )
    print(header)
    print("-" * len(header))
    started = time.perf_counter()
    for name, _ in CONDITIONS:
        group = cases[name]
        if not group:
            continue
        score = await _score(name, group, args.data_dir, cfg, use_model=args.model)
        print(
            f"{score.condition:18} {score.cases:>5} {score.recall:>9.1%} {score.correct:>9.1%}"
            f" {score.resolved:>9.1%} {score.fast_path:>8.1%} {score.requests:>5}"
        )
    print(f"\n所要 {time.perf_counter() - started:.1f} 秒")


if __name__ == "__main__":
    asyncio.run(main())
