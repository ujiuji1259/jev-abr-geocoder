"""解決状態から結果を組み立てる。

段（候補生成 → 町字 → 番号）が埋めた :class:`Resolution` を読み、``config.py``
の閾値と突き合わせて :class:`GeocodeResult` を作る。

**確信が持てないときも結果を捨てず、粒度を 1 段上げて返す**のがこの層の仕事。

====================  ==============================================
確信が持てないもの      返すもの
====================  ==============================================
番号                   町字の代表点、``granularity=MACHIAZA``
町字                   市区町村の代表点、``granularity=CITY``
市区町村               都道府県の代表点、``granularity=PREF``
何も当たらない          ``granularity=UNKNOWN``
====================  ==============================================

``resolved`` は「要求された粒度まで確信を持って解決できたか」、``granularity`` は
「実際にどこまで解決したか」を表す。この 2 つを分けてあるので、呼び出し側が
「町字まででよい」用途にそのまま使える（docs/code-design.md §7）。

**値はすべて frozen。** 組み立ては ``dataclasses.replace`` で「決まったところまで
入った写し」を作りながら進める。書き込む順で結果が変わらないので、どの粒度で
返したのかが関数の戻り値だけで追える。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from .address import Banchi, BanchiKind, CityRecord, Granularity, MachiazaRecord, Point
from .config import GeocoderConfig
from .decision import Decision
from .index.machiaza_index import MachiazaIndex
from .match.banchi import BanchiCandidates
from .match.banchi_tail import BanchiTail
from .match.candidates import CandidateSet
from .outcome import GeocodeResult

__all__ = ["Resolution", "build"]


@dataclass(frozen=True, slots=True)
class Resolution:
    """1 件分の解決状態。

    段は書き換えずに :meth:`dataclasses.replace` で写しを作って返す。**途中状態を
    共有して書き換えないので、どの段が何を決めたのかが戻り値に出る。**
    """

    query: str
    normalized: str
    candidates: CandidateSet
    machiaza: MachiazaRecord | None = None
    machiaza_decision: Decision | None = None
    tail: BanchiTail | None = None
    banchi: BanchiCandidates | None = None
    banchi_decision: Decision | None = None
    #: 入力が親番までで、枝番は ABR にしか無い場合 True。
    banchi_parent: bool = False
    #: 解決できなかった理由。
    note: str = ""

    def noted(self, note: str) -> Resolution:
        """理由を付けた写しを返す。**後段の理由で上書きしない。**

        判定モデルが落ちた等の根本的な理由が先に入るので、そちらを残すほうが
        役に立つ。「確信度が低い」はその結果にすぎない。空文字は無視する。
        """
        if self.note or not note:
            return self
        return replace(self, note=note)

    def machiaza_is_confident(self, cfg: GeocoderConfig) -> bool:
        """町字を確定したと見なせるか。番号を引きに行くかもこれで決まる。"""
        decision = self.machiaza_decision
        if self.machiaza is None or decision is None:
            return False
        if decision.fast_path:
            return True
        return (
            decision.confidence >= cfg.machiaza_confidence
            and decision.contains_answer >= cfg.present_threshold
        )


def build(item: Resolution, index: MachiazaIndex, cfg: GeocoderConfig) -> GeocodeResult:
    """解決状態を結果にする。"""
    # 段の側で溜まった理由を種にする。以降は結果の側に積む。
    result = GeocodeResult(query=item.query, normalized=item.normalized, note=item.note)
    machiaza = item.machiaza

    if machiaza is None:
        return _coarse(result, item, index)

    if not item.machiaza_is_confident(cfg):
        result = _at_city(result, machiaza, index.city(machiaza.lg_code))
        if item.machiaza_decision is None:
            return result
        return _noted(
            result, _low_confidence_note("町字", item.machiaza_decision, machiaza.name.display)
        )

    return _with_banchi(_at_machiaza(result, item, machiaza, index), item, cfg)


# ------------------------------------------------------------------ 各粒度


def _at_machiaza(
    result: GeocodeResult, item: Resolution, machiaza: MachiazaRecord, index: MachiazaIndex
) -> GeocodeResult:
    name = machiaza.name
    city = index.city(machiaza.lg_code)
    decision = item.machiaza_decision
    # ABR に町字の代表点が無ければ市区町村の点で代用する。何の点かは出力に残す。
    point = machiaza.point or (city.point if city else None)
    return replace(
        result,
        pref=name.pref,
        county=name.county,
        city=name.city,
        ward=name.ward,
        machiaza=name.machiaza,
        lg_code=machiaza.lg_code_str,
        machiaza_id=machiaza.machiaza_code,
        point=point,
        point_granularity=_source_of(point, machiaza.point, Granularity.MACHIAZA),
        granularity=Granularity.MACHIAZA,
        resolved=True,
        confidence=decision.confidence if decision else 0.0,
        probability=decision.probability if decision else 0.0,
        remainder=item.tail.raw if item.tail else "",
    )


def _at_city(
    result: GeocodeResult, machiaza: MachiazaRecord, city: CityRecord | None
) -> GeocodeResult:
    """町字までは絞れたが確信が持てない。市区町村として返す。"""
    name = machiaza.name
    point = city.point if city else machiaza.point
    return replace(
        result,
        pref=name.pref,
        county=name.county,
        city=name.city,
        ward=name.ward,
        lg_code=machiaza.lg_code_str,
        point=point,
        # 市区町村の点が無ければ町字の点を使う。粒度は答えより細かくなる。
        point_granularity=_source_of(
            point, city.point if city else None, Granularity.CITY, Granularity.MACHIAZA
        ),
        granularity=Granularity.CITY,
        resolved=False,
    )


def _with_banchi(result: GeocodeResult, item: Resolution, cfg: GeocoderConfig) -> GeocodeResult:
    decision = item.banchi_decision
    options = item.banchi
    if decision is None or options is None or not options:
        return result
    if decision.index is None or not 0 <= decision.index < len(options.entries):
        if decision.contains_answer and decision.contains_answer < cfg.present_threshold:
            return _noted(result, "番号が候補に見つからない")
        return result

    entry = options.entries[decision.index]
    if not decision.fast_path and decision.confidence < cfg.banchi_confidence:
        return _noted(result, _low_confidence_note("番号", decision, entry.display))

    numbers = item.tail.numbers if (item.banchi_parent and item.tail) else entry.numbers
    granularity = _granularity_for(options.kind, numbers)
    result = replace(
        result,
        banchi=_format_banchi(options.kind, numbers),
        granularity=granularity,
        confidence=decision.confidence,
        probability=decision.probability,
        point=entry.point or result.point,
        # 番号に座標があればそれが一番細かい。無ければ町字の点のまま。
        point_granularity=granularity if entry.point else result.point_granularity,
        remainder=_remainder_after_banchi(item.tail.raw if item.tail else "", entry),
    )

    if item.banchi_parent:
        # 枝番は入力に無いので ABR の ID は付けない。座標は代表のもの。
        return _noted(result, f"枝番は入力に含まれない。{len(options.entries)} 件のうち代表の座標")
    if options.kind is BanchiKind.PARCEL:
        return replace(result, prc_id=entry.prc_id())
    if options.kind is BanchiKind.RSDT:
        return replace(
            result,
            blk_id=entry.blk_id(),
            rsdt_id=entry.rsdt_id(),
            rsdt2_id=entry.rsdt2_id(),
        )
    return replace(result, blk_id=entry.blk_id())


def _coarse(result: GeocodeResult, item: Resolution, index: MachiazaIndex) -> GeocodeResult:
    """町字が決まらなかったとき、分かるところまでを返す。"""
    city_lg_code = item.candidates.city_lg_code
    if city_lg_code is not None:
        city = index.city(city_lg_code)
        if city is not None:
            return _ends_here(
                replace(
                    result,
                    pref=city.pref,
                    county=city.county,
                    city=city.city,
                    ward=city.ward,
                    lg_code=f"{city.lg_code:06d}",
                    point=city.point,
                    point_granularity=_source_of(city.point, city.point, Granularity.CITY),
                    granularity=Granularity.CITY,
                ),
                item,
                Granularity.CITY,
                "町字",
            )

    pref_lg_code = item.candidates.pref_lg_code
    if pref_lg_code is not None:
        pref = index.pref(pref_lg_code)
        if pref is not None:
            return _ends_here(
                replace(
                    result,
                    pref=pref.pref,
                    lg_code=f"{pref.lg_code:06d}",
                    point=pref.point,
                    point_granularity=_source_of(pref.point, pref.point, Granularity.PREF),
                    granularity=Granularity.PREF,
                ),
                item,
                Granularity.PREF,
                "市区町村",
            )

    return _noted(replace(result, granularity=Granularity.UNKNOWN), "候補が見つからない")


def _ends_here(
    result: GeocodeResult, item: Resolution, granularity: Granularity, missing: str
) -> GeocodeResult:
    """入力がこの粒度で尽きていたなら解決済みとし、そうでなければ理由を残す。"""
    if item.candidates.ends_at is granularity:
        return replace(result, resolved=True, confidence=1.0, probability=1.0)
    return _noted(result, f"{missing}を特定できない")


# -------------------------------------------------------------------- 表記


def _source_of(
    point: Point | None,
    own: Point | None,
    granularity: Granularity,
    fallback: Granularity = Granularity.CITY,
) -> Granularity:
    """``point`` が何の代表点かを返す。

    ``own`` はその粒度自身が持っていた点。``point`` がそれと同じなら
    ``granularity``、代用に落ちていれば ``fallback``、座標が無ければ UNKNOWN。
    """
    if point is None:
        return Granularity.UNKNOWN
    return granularity if own is not None else fallback


def _noted(result: GeocodeResult, note: str) -> GeocodeResult:
    """理由を付けた写しを返す。最初に付いたものを残す。"""
    if result.note or not note:
        return result
    return replace(result, note=note)


def _low_confidence_note(what: str, decision: Decision, guess: str) -> str:
    return f"{what}の確信度が低い ({decision.confidence:.2f}): 最有力は {guess}"


def _granularity_for(kind: BanchiKind, numbers: Sequence[int]) -> Granularity:
    """入力がどこまで特定できたかに応じた粒度。

    住居表示で街区しか与えられていないとき (「面影一丁目1番」) は、住居番号
    ではなく街区として返す。地番は枝番が無くても地番のまま。
    """
    if kind is BanchiKind.RSDT and len(tuple(numbers)) < 2:
        return Granularity.BLOCK
    return kind.granularity


def _format_banchi(kind: BanchiKind, numbers: Sequence[int]) -> str:
    parts: tuple[int, ...] = tuple(numbers)
    if len(parts) == 0:
        return ""
    if kind is BanchiKind.BLOCK:
        return f"{parts[0]}番"
    if kind is BanchiKind.RSDT:
        if len(parts) < 2:
            return f"{parts[0]}番"
        out = f"{parts[0]}番{parts[1]}号"
        return out + (f"の{parts[2]}" if len(parts) > 2 and parts[2] else "")
    return "-".join(str(n) for n in parts) + "番地"


_DIGITS = re.compile(r"\d+")
#: 番号の直後に付く助数詞。採用した番号と一緒に消費する。
_TRAILING_UNITS = ("丁目", "番地", "番", "号室", "号", "地割", "の", "ノ")
_REMAINDER_TRIM = "-ー−–—―‐ 　,、"


def _remainder_after_banchi(tail: str, entry: Banchi) -> str:
    """番号として消費した部分より後ろを残りとして返す。

    どこまでが住所かは判定モデルが決めているので、ここでは採用された番号の
    個数分だけ数値を読み飛ばし、最後の数値に続く助数詞も一緒に落とす。
    """
    pos = 0
    for _ in range(len(entry.numbers)):
        match = _DIGITS.search(tail, pos)
        if match is None:
            return ""
        pos = match.end()
    remainder = tail[pos:]
    for unit in _TRAILING_UNITS:
        if remainder.startswith(unit):
            remainder = remainder[len(unit) :]
            break
    return remainder.strip(_REMAINDER_TRIM)
