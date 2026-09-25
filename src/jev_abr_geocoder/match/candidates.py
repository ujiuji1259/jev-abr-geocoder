"""層1 の候補生成。

**トライは再現率だけを担保する。** 正解を候補集合に含めることにだけ責任を持ち、
どれが正解かの判断はしない（docs/architecture.md 原則1）。

``TownCandidate.score`` は 255 件に収まらないときに何を落とすかを決めるためだけ
のもので、「スコアが閾値以上なら採用」という判断には使わない。それを始めると
閾値調整が終わらなくなり、判断が Jev とトライに二重化する。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import GeocoderConfig
from ..index.townindex import TownIndex
from ..models import Level, TownCandidate

__all__ = ["CandidateFinder", "CandidateSet", "prefix_distance"]

#: フォールバックの編集距離スキャンで舐める鍵数の上限。
#: 1 市区町村の町字鍵はふつう数百〜数千で、政令市でも数万には届かない。
_SCAN_LIMIT = 40_000


@dataclass(frozen=True, slots=True)
class CandidateSet:
    candidates: list[TownCandidate]
    #: 前方一致で取れたか、フォールバックに落ちたか。診断用。
    exact: bool
    #: 市区町村までは特定できた場合、その city_id
    city_id: int | None = None
    #: 都道府県までは特定できた場合、その lg_code
    pref_lg_code: int | None = None
    #: 入力がこの粒度で終わっており、それ以上細かく探す余地が無い場合に設定する。
    #: このとき候補は空だが、その粒度としては確信を持って解決できている。
    exhausted: Level | None = None

    def __bool__(self) -> bool:
        return bool(self.candidates)

    def unambiguous(self) -> TownCandidate | None:
        """選ぶ余地が無い候補があればそれを返す。

        前方一致で取れていて、**厳密に最も長く一致した候補が 1 件だけ**のとき、
        それを返す。「大阪府高槻市奈佐原2丁目」と「大阪府高槻市大字奈佐原」なら
        前者。これは類似度の判断ではなく前方一致の定義そのものなので、
        トライが判断しているわけではない。

        同じ長さで複数が並ぶ場合（京都市中京区の同名町が 4 つある等）と、
        曖昧一致に落ちた場合は ``None`` を返して Jev に委ねる。
        """
        if not self.exact or not self.candidates:
            return None
        longest = max(len(c.matched) for c in self.candidates)
        top = [c for c in self.candidates if len(c.matched) == longest]
        return top[0] if len(top) == 1 else None


def prefix_distance(pattern: str, text: str, max_distance: int) -> int:
    """``pattern`` と ``text`` の「前方一致としての」編集距離。

    ``text`` は途中で終わってよい（末尾に番地や建物名が続くため）。通常の
    Levenshtein の DP を回し、最終行ではなく **最終列の最小値** を取る。

    ``max_distance`` を超えることが確定した時点で打ち切り、
    ``max_distance + 1`` を返す。
    """
    n = len(pattern)
    if n == 0:
        return 0
    limit = max_distance
    previous = list(range(n + 1))
    best_tail = previous[n]
    for i, ch in enumerate(text, start=1):
        current = [i] + [0] * n
        row_min = current[0]
        for j in range(1, n + 1):
            cost = 0 if pattern[j - 1] == ch else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if current[j] < row_min:
                row_min = current[j]
        if current[n] < best_tail:
            best_tail = current[n]
        if row_min > limit:
            # この行以降、距離は単調に増えるだけなので打ち切ってよい。
            break
        previous = current
    return best_tail if best_tail <= limit else limit + 1


class CandidateFinder:
    def __init__(self, index: TownIndex, cfg: GeocoderConfig) -> None:
        self._index = index
        self._cfg = cfg

    def find(self, normalized: str) -> CandidateSet:
        """正規化済み入力から町字候補を出す。"""
        if not normalized:
            return CandidateSet(candidates=[], exact=False)

        exact = self._exact(normalized)
        if exact:
            return exact
        return self._fallback(normalized)

    # ----------------------------------------------------------- 前方一致

    def _exact(self, normalized: str) -> CandidateSet | None:
        hits = self._index.prefixes(normalized)
        if not hits:
            return None
        seen: set[int] = set()
        out: list[TownCandidate] = []
        for hit in hits:  # 長い順
            if hit.town_id in seen:
                continue
            seen.add(hit.town_id)
            out.append(
                TownCandidate(
                    town_id=hit.town_id,
                    matched=hit.key,
                    remainder=normalized[hit.matched_len :],
                    # 長く一致したものほど上位。同点は挿入順。
                    score=hit.matched_len / len(normalized),
                )
            )
            if len(out) >= self._cfg.max_options:
                break
        city = self._index.city_prefixes(normalized)
        return CandidateSet(candidates=out, exact=True, city_id=city[0].city_id if city else None)

    # --------------------------------------------------------- 曖昧一致

    def _fallback(self, normalized: str) -> CandidateSet:
        """前方一致が取れなかったとき。

        市区町村まで前方一致すれば、その配下の町字鍵だけを舐めればよいので、
        母集合は数百〜数千に収まる。市区町村も当たらなければ、1,918 件の
        市区町村エイリアスを舐めて一番近いものを選ぶ。
        """
        pref_hit = self._index.pref_prefix(normalized)
        if pref_hit is not None and len(pref_hit[1]) >= len(normalized):
            # 入力が都道府県までで終わっている。
            return CandidateSet(
                candidates=[],
                exact=True,
                pref_lg_code=pref_hit[0].lg_code,
                exhausted=Level.PREF,
            )

        city_hits = self._index.city_prefixes(normalized)
        if city_hits:
            best = city_hits[0]
            if best.matched_len >= len(normalized):
                # 入力が市区町村までで終わっている。町字を探す余地が無い。
                return CandidateSet(
                    candidates=[], exact=True, city_id=best.city_id, exhausted=Level.CITY
                )
            return CandidateSet(
                candidates=self._scan_under(best.key, normalized),
                exact=False,
                city_id=best.city_id,
            )

        city_id, city_key = self._nearest_city(normalized)
        if city_id is not None and city_key is not None:
            return CandidateSet(
                candidates=self._scan_under(city_key, normalized),
                exact=False,
                city_id=city_id,
            )

        pref = self._index.pref_prefix(normalized)
        return CandidateSet(
            candidates=[], exact=False, pref_lg_code=pref[0].lg_code if pref else None
        )

    def _scan_under(self, city_key: str, normalized: str) -> list[TownCandidate]:
        keys = self._index.keys_under(city_key, _SCAN_LIMIT)
        if not keys:
            return []
        max_distance = self._cfg.max_edit_distance
        scored: dict[int, tuple[int, str]] = {}
        for key, town_id in keys:
            distance = prefix_distance(key, normalized, max_distance)
            if distance > max_distance:
                continue
            current = scored.get(town_id)
            if current is None or distance < current[0]:
                scored[town_id] = (distance, key)
        if not scored:
            return []

        ranked = sorted(scored.items(), key=lambda item: (item[1][0], -len(item[1][1])))
        limit = min(self._cfg.fallback_limit, self._cfg.max_options)
        out: list[TownCandidate] = []
        for town_id, (distance, key) in ranked[:limit]:
            out.append(
                TownCandidate(
                    town_id=town_id,
                    matched=key,
                    remainder=_remainder_after(key, normalized),
                    score=1.0 / (1.0 + distance),
                )
            )
        return out

    def _nearest_city(self, normalized: str) -> tuple[int | None, str | None]:
        """市区町村エイリアスのうち、入力の先頭に最も近いもの。約 8k 件の線形走査。"""
        max_distance = self._cfg.max_edit_distance
        best: tuple[int, int, int, str] | None = None
        for key, city_id in self._index.city_alias_pairs:
            distance = prefix_distance(key, normalized, max_distance)
            if distance > max_distance:
                continue
            ranking = (distance, -len(key), city_id, key)
            if best is None or ranking < best:
                best = ranking
        if best is None:
            return None, None
        return best[2], best[3]


def _remainder_after(key: str, normalized: str) -> str:
    """曖昧一致した鍵のぶんだけ入力を進めた残り。

    文字数がずれている可能性があるので、鍵と同じ長さで切るのではなく、
    前方一致としての最良の切れ目を探す。
    """
    best_cut = min(len(key), len(normalized))
    best_distance = prefix_distance(key, normalized[:best_cut], len(key))
    span = 3
    low = max(0, best_cut - span)
    high = min(len(normalized), best_cut + span)
    for cut in range(low, high + 1):
        distance = prefix_distance(key, normalized[:cut], len(key))
        if distance < best_distance:
            best_distance = distance
            best_cut = cut
    return normalized[best_cut:]
