"""層1 の候補生成。

**トライは再現率だけを担保する。** 正解を候補集合に含めることにだけ責任を持ち、
どれが正解かの判断はしない（docs/architecture.md 原則1）。

絞り込みは**完全一致だけ**で行い、類似度の計算はしない。

1. **索引鍵が入力の先頭** — ふつうの前方一致。入力の 99.6% はここで決まる
2. **入力が市区町村・都道府県ちょうどで終わる** — 町字を探す余地が無い
3. **入力が索引鍵の先頭** — 入力が途中で終わっている（丁目や小字の省略）。
   末尾の番地を削ってからもう一度試す
4. **どれも当たらない** — その市区町村の町字を**全部** Jev に渡して選ばせる。
   誤字・異体字はここ。どれが入力の意図かを選ぶのはまさに Jev の仕事なので、
   こちらでは絞り込まない
5. 町字数が 255 件を超えて渡しきれない市区町村だけ、粒度を落とす

**編集距離は持たない。** 以前は 4 の代わりに編集距離で候補を絞っていたが、
実測で割に合わなかった。geolonia の難例 7,191 件で、曖昧一致に落ちるのは
0.3%、そのうち正解を候補に含められたのは 44%。実質 0.15% を拾うために
全件のレイテンシが 25 倍（17 µs → 433 µs）になり、さらに「叶」「嶋」「新」の
ような 1 文字の町名が距離 1 で上位を占めて候補集合を汚していた。

4 はその置き換えで、**絞り込みを諦めて判断を Jev に渡す**ほうが筋が良い。
異体字の同定は Jev の得意分野であり、こちらが距離で順位づけする筋合いがない。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from ..address import Granularity, MachiazaRecord
from ..config import GeocoderConfig
from ..index.machiaza_index import MachiazaIndex

__all__ = [
    "CandidateFinder",
    "CandidateSet",
    "group_by_display",
    "group_by_oaza",
]

#: 末尾の番地らしき部分。入力が索引鍵の先頭かを試す前にここだけ削る。
_TRAILING_NUMBER = re.compile(r"[\d\-ー−–—―‐]+$")


def _first_digit_after(text: str, start: int) -> int:
    """``start`` 以降で最初に数字が現れる位置。無ければ文字列長。"""
    match = re.compile(r"\d").search(text, start)
    return match.start() if match else len(text)


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
class MachiazaCandidate:
    """層1 が出した町字候補。

    **並び順が順位。** 索引に近いものから並び、上限を超えた分は後ろから落とす。
    スコアは持たない。数値を持たせると「この値以上なら採用」と書きたくなり、
    採否の判断が判定モデルからこちら側に漏れる（docs/architecture.md 原則1）。
    """

    row_id: int
    #: 入力のうち町字として消費した部分
    matched: str
    #: 残り（数値テール + 建物名）
    remainder: str


@dataclass(frozen=True, slots=True)
class CandidateSet:
    candidates: list[MachiazaCandidate]
    #: 索引鍵が入力の先頭として一致したか。診断用。
    prefix_matched: bool
    #: 市区町村までは特定できた場合、その lg_code
    city_lg_code: int | None = None
    #: 都道府県までは特定できた場合、その lg_code
    pref_lg_code: int | None = None
    #: 入力がこの粒度で終わっており、それ以上細かく探す余地が無い場合に設定する。
    #: このとき候補は空だが、その粒度としては確信を持って解決できている。
    ends_at: Granularity | None = None
    #: 候補が Choice の上限を超えており、Jev で分割絞り込み が要る。
    #: このとき candidates は上限を超えた件数を持つので、そのままでは渡せない。
    needs_narrowing: bool = False

    def narrowed(self, candidates: list[MachiazaCandidate]) -> CandidateSet:
        """絞り込んだ候補で置き換える。

        分割絞り込み の結果を受ける口。絞り終わっているので
        ``needs_narrowing`` は下りる。空を渡せば「候補なし」になり、粒度が落ちる。
        """
        return replace(self, candidates=candidates, needs_narrowing=False)

    def unambiguous(self) -> MachiazaCandidate | None:
        """選ぶ余地が無い候補があればそれを返す。

        索引鍵が入力の先頭として一致していて、**厳密に最も長く一致した候補が
        1 件だけ**のとき、それを返す。「大阪府高槻市奈佐原2丁目」と
        「大阪府高槻市大字奈佐原」なら前者。これは類似度の判断ではなく
        前方一致の定義そのものなので、トライが決めてよい。

        同じ長さで複数が並ぶ場合（京都市中京区に同名の町字が 4 つある等）は
        ``None`` を返して Jev に委ねる。
        """
        if not self.prefix_matched or not self.candidates:
            return None
        longest = max(len(c.matched) for c in self.candidates)
        top = [c for c in self.candidates if len(c.matched) == longest]
        return top[0] if len(top) == 1 else None


class CandidateFinder:
    def __init__(self, index: MachiazaIndex, cfg: GeocoderConfig) -> None:
        self._index = index
        self._cfg = cfg

    def find(self, normalized: str) -> CandidateSet:
        """正規化済み入力から町字候補を出す。"""
        if not normalized:
            return CandidateSet(candidates=[], prefix_matched=False)
        # 候補が空でも意味のある結果（市区町村で確定など）を返す段があるので、
        # 真偽値ではなく None かどうかで分岐する。
        for step in (self._forward, self._boundary, self._extensions, self._city_wide):
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
        out: list[MachiazaCandidate] = []
        for hit in hits:  # 長い順
            if hit.row_id in seen or _splits_a_number(hit.key, normalized):
                continue
            seen.add(hit.row_id)
            out.append(
                # 長く一致したものほど上位。同点は索引の順。
                MachiazaCandidate(
                    row_id=hit.row_id,
                    matched=hit.key,
                    remainder=normalized[hit.matched_len :],
                )
            )
            if len(out) >= self._cfg.max_candidates:
                break
        city = self._index.city_prefixes(normalized)
        return CandidateSet(
            candidates=out, prefix_matched=True, city_lg_code=city[0].lg_code if city else None
        )

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
                prefix_matched=True,
                city_lg_code=city_hits[0].lg_code,
                ends_at=Granularity.CITY,
            )
        pref_hit = self._index.pref_prefix(normalized)
        if pref_hit is not None and len(pref_hit[1]) >= len(normalized):
            return CandidateSet(
                candidates=[],
                prefix_matched=True,
                pref_lg_code=pref_hit[0].lg_code,
                ends_at=Granularity.PREF,
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
            keys = self._index.machiaza_extensions(stem, self._cfg.max_candidates * 4)
            if not keys:
                continue
            # 入力からの継ぎ足しが短いものほど「言いかけ」に近い。
            # 長さで切るのは類似度の判断ではなく、255 件に収める機械的な規則。
            keys.sort(key=lambda item: (len(item[0]), item[0]))
            seen: set[int] = set()
            out: list[MachiazaCandidate] = []
            remainder = normalized[len(stem) :]
            for _key, row_id in keys:
                if row_id in seen:
                    continue
                seen.add(row_id)
                out.append(MachiazaCandidate(row_id=row_id, matched=stem, remainder=remainder))
                if len(out) >= self._cfg.max_candidates:
                    break
            city = self._index.city_prefixes(normalized)
            return CandidateSet(
                candidates=out, prefix_matched=False, city_lg_code=city[0].lg_code if city else None
            )
        return None

    # --------------------------------- ④ 市区町村配下を総当たりで Jev に渡す

    def _city_wide(self, normalized: str) -> CandidateSet | None:
        """完全一致がどれも当たらなかったとき、その市区町村の町字を全部渡す。

        誤字・異体字（「箪笥町」と「簞笥町」、「緑が浜」と「緑ヶ浜」、
        「7番町」と「七番丁」）はここに来る。**どれが入力の意図かを選ぶのは
        まさに Jev の仕事**なので、絞り込みをせずに選択肢として並べる。

        ここでは順位づけも足切りもしない。市区町村が決まっていれば母集団は
        高々その町字数で、74% の市区町村は 255 件に収まる。

        収まらない場合（26%、最大は福井市の 15,399 件）は ``needs_narrowing`` を
        立てて全件を持ち帰る。何を落とすかの判断はやはりこちらではせず、
        分割して Jev に「この一覧の中にあるか」を並列に訊いて絞る。
        """
        city_hits = self._index.city_prefixes(normalized)
        if not city_hits:
            return None
        best = city_hits[0]
        limit = self._cfg.max_candidates
        ceiling = self._cfg.narrow_max_machiaza if self._cfg.narrow_by_oaza else limit
        keys = self._index.machiaza_extensions(best.key, (ceiling + 1) * 8)
        seen: dict[int, str] = {}
        for key, row_id in keys:
            if row_id not in seen:
                seen[row_id] = key
            if len(seen) > ceiling:
                # 分割絞り込みでも扱いきれない規模。粒度を落とす。
                return None
        if not seen:
            return None

        # 町字がどこで終わるかは異体字のせいで文字単位には合わせられない。
        # 住所は町字の直後に番地が来るので、市区町村より後ろの最初の数字を
        # 切れ目とする。文字の照合ではなく位置の規則。
        cut = _first_digit_after(normalized, best.matched_len)
        remainder = normalized[cut:]
        matched = normalized[:cut]
        candidates = [
            MachiazaCandidate(row_id=row_id, matched=matched, remainder=remainder)
            for row_id in seen
        ]
        return CandidateSet(
            candidates=candidates,
            prefix_matched=False,
            city_lg_code=best.lg_code,
            needs_narrowing=len(candidates) > limit,
        )

    # ------------------------------------------- ⑤ 粒度を落とす

    def _coarse(self, normalized: str) -> CandidateSet:
        """町字が取れず、市区町村の町字を並べることもできないとき。

        町字数が 255 件を超える市区町村（全体の 26%、最大は福井市の 15,399 件）
        で誤字が来た場合がここ。何を候補から落とすかの判断が必要になるので、
        今は手を出さずに市区町村の粒度で返す。
        """
        city_hits = self._index.city_prefixes(normalized)
        if city_hits:
            best = city_hits[0]
            ends_at = Granularity.CITY if best.matched_len >= len(normalized) else None
            return CandidateSet(
                candidates=[], prefix_matched=False, city_lg_code=best.lg_code, ends_at=ends_at
            )

        pref_hit = self._index.pref_prefix(normalized)
        if pref_hit is not None:
            ends_at = Granularity.PREF if len(pref_hit[1]) >= len(normalized) else None
            return CandidateSet(
                candidates=[],
                prefix_matched=False,
                pref_lg_code=pref_hit[0].lg_code,
                ends_at=ends_at,
            )
        return CandidateSet(candidates=[], prefix_matched=False)


# ------------------------------------------------- 候補のまとめ方


def group_by_display(
    candidates: Sequence[MachiazaCandidate], records: dict[int, MachiazaRecord]
) -> list[list[MachiazaCandidate]]:
    """表示住所ごとにまとめる。出現順を保つ。

    **表示が同じ候補を判定モデルに重ねて見せない。** 京都市中京区には同名の
    「大文字町」が 4 つあり、そのまま並べると同一文字列の選択肢が 4 つ並ぶ。
    答えようがないので確信度が割れ、住所としては正しいのに閾値を下回って
    粒度が落ちていた。
    """
    return _group(candidates, lambda record: record.name.display, records)


def group_by_oaza(
    candidates: Sequence[MachiazaCandidate], records: dict[int, MachiazaRecord]
) -> list[list[MachiazaCandidate]]:
    """大字ごとにまとめる。出現順を保つ。

    候補が上限を超えた市区町村で、先に大字だけを選ばせるときの単位。
    """
    return _group(candidates, lambda record: record.name.oaza_display, records)


def _group(
    candidates: Sequence[MachiazaCandidate],
    key: Callable[[MachiazaRecord], str],
    records: dict[int, MachiazaRecord],
) -> list[list[MachiazaCandidate]]:
    groups: dict[str, list[MachiazaCandidate]] = {}
    for candidate in candidates:
        groups.setdefault(key(records[candidate.row_id]), []).append(candidate)
    return list(groups.values())
