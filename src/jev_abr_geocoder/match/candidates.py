"""層1 の候補生成。

**トライは再現率だけを担保する。** 正解を候補集合に含めることにだけ責任を持ち、
どれが正解かの判断はしない（docs/architecture.md 原則1）。

探索は 2 方向の**完全一致**だけで、曖昧一致はしない。

1. **索引鍵が入力の先頭** — ふつうの前方一致。入力の 99.6% はここで決まる
2. **入力が市区町村・都道府県ちょうどで終わる** — 町字を探す余地が無い
3. **入力が索引鍵の先頭** — 入力が途中で終わっている（丁目や小字の省略）。
   末尾の番地を削ってからもう一度試す

どちらも当たらない誤字・異体字は、**町字を諦めて市区町村の粒度で返す**。

以前は編集距離によるフォールバックを持っていたが、実測で割に合わなかった。
geolonia の難例 7,191 件では、曖昧一致に落ちるのは 0.3% でそのうち正解を
候補に含められたのは 44%。実質 0.15% を拾うために全件のレイテンシが
25 倍（17 µs → 433 µs）になり、さらに「叶」「嶋」「新」のような 1 文字の町名が
距離 1 で上位を占めて候補集合を汚していた。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import GeocoderConfig
from ..index.townindex import TownIndex
from ..models import Level, TownCandidate

__all__ = ["CandidateFinder", "CandidateSet"]

#: 末尾の番地らしき部分。入力が索引鍵の先頭かを試す前にここだけ削る。
_TRAILING_NUMBER = re.compile(r"[\d\-ー−–—―‐]+$")


def _splits_a_number(key: str, text: str) -> bool:
    """索引鍵が入力の数字列を途中で切っているか。

    丁目を省略したエイリアス（「鎌倉市岡本1」）は、地番の先頭の数字にも
    当たってしまう。「鎌倉市岡本1189-4」は *大字岡本の 1189 番地* であって
    *岡本一丁目の 189* ではないのに、長いほうが勝つ規則のせいで誤って
    一丁目に確定していた。

    数字列はそれ自体が 1 つのトークンなので、その途中で切れる一致は成立しない。
    これは類似度の判断ではなく字句の規則。
    """
    tail = text[len(key) : len(key) + 1]
    return bool(key) and key[-1].isdigit() and tail.isdigit()


@dataclass(frozen=True, slots=True)
class CandidateSet:
    candidates: list[TownCandidate]
    #: 索引鍵が入力の先頭として一致したか。診断用。
    exact: bool
    #: 市区町村までは特定できた場合、その city_id
    city_id: int | None = None
    #: 都道府県までは特定できた場合、その lg_code
    pref_lg_code: int | None = None
    #: 入力がこの粒度で終わっており、それ以上細かく探す余地が無い場合に設定する。
    #: このとき候補は空だが、その粒度としては確信を持って解決できている。
    exhausted: Level | None = None

    def unambiguous(self) -> TownCandidate | None:
        """選ぶ余地が無い候補があればそれを返す。

        索引鍵が入力の先頭として一致していて、**厳密に最も長く一致した候補が
        1 件だけ**のとき、それを返す。「大阪府高槻市奈佐原2丁目」と
        「大阪府高槻市大字奈佐原」なら前者。これは類似度の判断ではなく
        前方一致の定義そのものなので、トライが決めてよい。

        同じ長さで複数が並ぶ場合（京都市中京区に同名の町字が 4 つある等）は
        ``None`` を返して Jev に委ねる。
        """
        if not self.exact or not self.candidates:
            return None
        longest = max(len(c.matched) for c in self.candidates)
        top = [c for c in self.candidates if len(c.matched) == longest]
        return top[0] if len(top) == 1 else None


class CandidateFinder:
    def __init__(self, index: TownIndex, cfg: GeocoderConfig) -> None:
        self._index = index
        self._cfg = cfg

    def find(self, normalized: str) -> CandidateSet:
        """正規化済み入力から町字候補を出す。"""
        if not normalized:
            return CandidateSet(candidates=[], exact=False)
        # 候補が空でも意味のある結果（市区町村で確定など）を返す段があるので、
        # 真偽値ではなく None かどうかで分岐する。
        for step in (self._forward, self._boundary, self._extensions):
            found = step(normalized)
            if found is not None:
                return found
        return self._coarse(normalized)

    # ------------------------------------------- ① 索引鍵が入力の先頭

    def _forward(self, normalized: str) -> CandidateSet | None:
        hits = self._index.prefixes(normalized)
        if not hits:
            return None
        seen: set[int] = set()
        out: list[TownCandidate] = []
        for hit in hits:  # 長い順
            if hit.town_id in seen or _splits_a_number(hit.key, normalized):
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
            if len(out) >= self._cfg.max_candidates:
                break
        city = self._index.city_prefixes(normalized)
        return CandidateSet(candidates=out, exact=True, city_id=city[0].city_id if city else None)

    # ------------------------------ ② 入力がちょうど市区町村・都道府県で終わる

    def _boundary(self, normalized: str) -> CandidateSet | None:
        """入力が市区町村または都道府県ちょうどで終わっている場合。

        「鳥取県鳥取市」は配下の全町字鍵の先頭でもあるが、町字を言いかけた
        わけではなく市区町村として完結している。③ より先に判定しないと、
        市区町村名だけの入力に町字候補が大量に付いてしまう。
        """
        city_hits = self._index.city_prefixes(normalized)
        if city_hits and city_hits[0].matched_len >= len(normalized):
            return CandidateSet(
                candidates=[],
                exact=True,
                city_id=city_hits[0].city_id,
                exhausted=Level.CITY,
            )
        pref_hit = self._index.pref_prefix(normalized)
        if pref_hit is not None and len(pref_hit[1]) >= len(normalized):
            return CandidateSet(
                candidates=[],
                exact=True,
                pref_lg_code=pref_hit[0].lg_code,
                exhausted=Level.PREF,
            )
        return None

    # ------------------------------------------- ③ 入力が索引鍵の先頭

    def _extensions(self, normalized: str) -> CandidateSet | None:
        """入力が途中で終わっているケース。

        「鳥取県鳥取市面影」は ABR に行が無く（丁目を持つ大字 22,797 件のうち
        丁目なしの親エントリも在るのは 19% だけ）、前方一致は取れない。しかし
        索引鍵の側がこの入力で始まっているので、``keys(prefix)`` で拾える。

        「京都府向日市鶏冠井町22-20」のように番地が付いている場合は、末尾の
        数値を削ってから試す。
        """
        stems = [normalized]
        stripped = _TRAILING_NUMBER.sub("", normalized)
        if stripped and stripped != normalized:
            stems.append(stripped)

        for stem in stems:
            keys = self._index.keys_under(stem, self._cfg.max_candidates * 4)
            if not keys:
                continue
            # 入力からの継ぎ足しが短いものほど「言いかけ」に近い。
            # 長さで切るのは類似度の判断ではなく、255 件に収める機械的な規則。
            keys.sort(key=lambda item: (len(item[0]), item[0]))
            seen: set[int] = set()
            out: list[TownCandidate] = []
            remainder = normalized[len(stem) :]
            for key, town_id in keys:
                if town_id in seen:
                    continue
                seen.add(town_id)
                out.append(
                    TownCandidate(
                        town_id=town_id,
                        matched=stem,
                        remainder=remainder,
                        score=len(stem) / max(1, len(key)),
                    )
                )
                if len(out) >= self._cfg.max_candidates:
                    break
            city = self._index.city_prefixes(normalized)
            return CandidateSet(
                candidates=out, exact=False, city_id=city[0].city_id if city else None
            )
        return None

    # ------------------------------------------- ④ 粒度を落とす

    def _coarse(self, normalized: str) -> CandidateSet:
        """町字が取れないとき、分かるところまでを返す。

        誤字・異体字（「箪笥町」と「簞笥町」、「緑が浜」と「緑ヶ浜」）はここに
        来る。かつては編集距離で拾おうとしていたが、再現率 44% に対して
        全件のレイテンシが 25 倍になるため止めた。
        """
        city_hits = self._index.city_prefixes(normalized)
        if city_hits:
            best = city_hits[0]
            exhausted = Level.CITY if best.matched_len >= len(normalized) else None
            return CandidateSet(
                candidates=[], exact=False, city_id=best.city_id, exhausted=exhausted
            )

        pref_hit = self._index.pref_prefix(normalized)
        if pref_hit is not None:
            exhausted = Level.PREF if len(pref_hit[1]) >= len(normalized) else None
            return CandidateSet(
                candidates=[],
                exact=False,
                pref_lg_code=pref_hit[0].lg_code,
                exhausted=exhausted,
            )
        return CandidateSet(candidates=[], exact=False)
