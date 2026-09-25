# コード設計

[docs/architecture.md](architecture.md) で決めた構成を、モジュール・型・インターフェースに落としたもの。

---

## 1. 設計を貫く3つの制約

この3つが、以下のモジュール分割とインターフェースのほぼすべてを決めている。

### 制約1: Jev 呼び出しは「バッチ全体で2回」

段 (a) 町字確定 と 段 (b) 番号確定 には依存関係があるので往復は2回必要だが、**入力が何件あっても2回で済ませる**。

これを型で強制する。`geocode()` は `geocode_many([q])[0]` として実装し、**単数形の専用経路を作らない**。単数経路があると「1件ずつループで呼ぶ」が自然に書けてしまい、バッチ性が静かに失われる。

### 制約2: 質問文と閾値は `config.py` にしか存在しない

TypeSafe のドキュメントが「人間がレビューすべきは質問と閾値だけ」と述べているとおり、これらが散らばるとレビュー不能になる。

**`config.py` 以外のファイルに、Jev に渡す文字列リテラルと比較用の定数を書かない。** レビュー時に `config.py` だけ読めば挙動が分かる状態を保つ。

### 制約3: 表記ゆれの知識は `index/keys.py` にしか存在しない

入力側の正規化は NFKC + 空白除去のみ。表記ゆれの吸収は**索引側のエイリアス生成**で行い、その規則は `index/keys.py` の1箇所に閉じる。

「入力がこう来たらこう直す」というコードを他のどこにも書かない。書きたくなったら、それは索引側の鍵を増やすべき場面である。

---

## 2. モジュール構成

```
src/jev_abr_geocoder/
├── __init__.py          公開 API: Geocoder, GeocodeResult, Level, GeocoderConfig
├── models.py            値型（すべて frozen dataclass）
├── config.py            ★ 質問文と閾値の唯一の置き場
├── textnorm.py          NFKC + 空白除去。これだけ
│
├── abr/                 ABR データの取得（索引の構造を知らない）
│   ├── catalog.py       DCAT フィード → FileRef 一覧
│   ├── fetch.py         ダウンロード・キャッシュ・更新判定
│   └── csvsrc.py        zip 内 CSV のストリーム読み
│
├── index/               永続化層（Jev を知らない）
│   ├── keys.py          ★ エイリアス鍵生成規則の唯一の置き場
│   ├── townindex.py     層1 読み書き: town.marisa + towns.bin
│   ├── numblob.py       層2 バイナリ仕様: エンコード / デコード
│   ├── store.py         層2 読み書き: abr.db
│   └── build.py         構築オーケストレーション（再開可能）
│
├── match/               候補生成と判定
│   ├── candidates.py    層1 の候補生成（前方一致 → フォールバック）
│   ├── tail.py          数値テール抽出
│   └── rerank.py        Jev 呼び出し（Choice + Noul）
│
├── geocoder.py          オーケストレーション
└── cli.py               薄いアダプタ
```

依存方向は上から下への一方向。`index/` は `match/` を知らず、`abr/` は索引の構造を知らない。`geocoder.py` だけが全部を知る。

---

## 3. 層1: `town.marisa` + `abr.db` の `town` テーブル

トライのペイロードは町字レコードへのインデックス (`<I`) だけを持つ。表示用の住所文字列と座標は `abr.db` の `town` テーブルに置く。

**理由**: 鍵は 2.27M 本あるが町字は 727k 件しかないので、トライにレコードを埋め込むと 3.1 倍冗長になる。また、トライの鍵は NFKC 正規化後のエイリアスであり、**出力に使うべき ABR の正規表記そのものではない**（「鳥取県鳥取市面影1丁目」ではなく「鳥取県鳥取市面影一丁目」を返したい）。

### `town` テーブル

```sql
CREATE TABLE town(
  town_id      INTEGER PRIMARY KEY,   -- トライのペイロードと一致
  lg_code      INTEGER NOT NULL,
  machiaza_id  INTEGER NOT NULL,
  pref         TEXT, county TEXT, city TEXT, ward TEXT,
  oaza_cho     TEXT, chome TEXT, koaza TEXT,
  rsdt_addr_flg INTEGER NOT NULL,
  status_flg   INTEGER NOT NULL,
  lat_1e7      INTEGER, lon_1e7 INTEGER
);
CREATE INDEX town_by_city ON town(lg_code, machiaza_id);
```

約 150 MB。`town_id` は `INTEGER PRIMARY KEY`（= rowid のエイリアス）なので、トライが返す ID からの引きは B-tree の直接引きになる。

行を引くのは**候補を 255 件に絞ったあと**だけなので、1 クエリあたり高々数百行。行デコードのコストは Jev の往復（70〜500 ms）に対して無視できる。

> mmap した固定長レコード配列（約 57 MB、ワーカー間でページキャッシュ共有）も検討したが、**構築と実装の単純さを優先して SQLite を採る**。層2 と同じ 1 ファイルに収まるので、配布物が `town.marisa` と `abr.db` の 2 つだけになる利点もある。

### インターフェース

```python
class TownIndex:
    """読み取り専用。プロセス間で安全に共有できる。"""

    @classmethod
    def open(cls, data_dir: Path) -> TownIndex:
        """town.marisa を mmap し、abr.db を読み取り専用で開く。"""

    def prefixes(self, text: str) -> list[TownHit]:
        """text の前方一致鍵をすべて返す。長い順。"""

    def under(self, prefix: str, limit: int) -> list[TownHit]:
        """prefix 配下の町字を返す。フォールバック用。"""

    def record(self, town_id: int) -> TownRecord: ...

    @property
    def meta(self) -> IndexMeta:
        """ABR の取得日時・件数・索引フォーマット版。"""
```

`TownHit` は `(town_id: int, matched_len: int)` の軽量タプル。`TownRecord` への変換は必要になってから行う（候補を 255 件に絞ったあとでよい）。

---

## 4. 層2: `abr.db`

```python
class NumberKind(IntEnum):
    BLOCK = 1
    RSDT = 2
    PARCEL = 3

class NumberStore:
    @classmethod
    def open(cls, data_dir: Path, *, readonly: bool = True) -> NumberStore: ...

    def fetch(
        self,
        lg_code: int,
        machiaza_id: int,
        kind: NumberKind,
        *,
        first: int | None = None,
        limit: int | None = None,
    ) -> list[NumberEntry]:
        """町字配下の番号を返す。

        first を与えるとチャンク目録で二分探索し、その第1番号を含む
        チャンクだけを展開する。町字の大きさによらず 0.2 ms 程度。
        """
```

```python
@dataclass(frozen=True, slots=True)
class NumberEntry:
    num1: int          # blk_num / blk_num / prc_num1
    num2: int          # -       / rsdt_num / prc_num2
    num3: int          # -       / -        / prc_num3
    lat: float | None
    lon: float | None
```

`blk_id` / `rsdt_id` / `prc_id` は番号のゼロ詰めであることを実測で確認済みなので格納せず、出力時に `models.py` のヘルパで復元する。

`numblob.py` は `encode(entries) -> bytes` と `decode(blob, first=None) -> list[NumberEntry]` の純関数2本だけを公開する。SQLite を知らないので、エンコード・デコードのラウンドトリップテストが単体で書ける。

---

## 5. 候補生成 `match/candidates.py`

```python
@dataclass(frozen=True, slots=True)
class TownCandidate:
    town_id: int
    matched: str        # 入力のうち消費した部分
    remainder: str      # 残り（数値テール + 建物名）
    score: float        # 語彙的スコア。順位付けにのみ使い、採否の判断には使わない
```

```python
class CandidateFinder:
    def __init__(self, index: TownIndex, cfg: GeocoderConfig): ...

    def find(self, normalized: str) -> list[TownCandidate]:
        """候補を最大 cfg.max_options 件返す。"""
```

探索の順序:

1. `index.prefixes(normalized)` — 5.3 µs。一致鍵をすべて取る
2. 0 件なら**段階的に短くしてフォールバック**する。都道府県+市区町村の前方一致を探し、`index.under(city_prefix)` でその配下だけ取り出して編集距離スキャン
3. 市区町村すら当たらなければ、1,918 件の市区町村全体に編集距離スキャン（数ミリ秒）

**`score` は順位付け専用。** 255 件に収まらないときにどれを落とすかを決めるためだけに使う。「スコアがこの値以上なら採用」という判断は書かない — それをやり始めると閾値調整が始まり、原則1 が崩れる。

---

## 6. Jev 呼び出し `match/rerank.py`

### 差し替え可能にする

```python
class DecisionModel(Protocol):
    async def ask(
        self,
        state: JSONContent,
        questions: Mapping[str, Question],
    ) -> Mapping[str, Answer]: ...
```

`typesafe-sdk` の `AsyncTypeSafeClient` を薄く包んだ `JevModel` が実装を持つ。テストでは `FakeModel` を差し込む。**これがテスト可能性の要**で、これがないと Jev なしでは何も検証できなくなる。

### バッチの組み立て

N 件の入力を1リクエストに詰める。`state` に全入力を置き、質問を N 個並列に並べ、`instructions` から `state` のキーを参照する（API リファレンスの「構造化された instructions」パターン）。

```python
state = {
    "inputs": {
        "q0": {"原文": "鳥取市面かげ1-2-3 〇〇マンション301"},
        "q1": {"原文": "..."},
    }
}
questions = {
    "q0":      Choice(instructions={...,"対象": "`inputs.q0`"}, criteria={...}),
    "q0_has":  Noul(instructions={...,"対象": "`inputs.q0`"}),
    "q1":      Choice(...),
    "q1_has":  Noul(...),
}
```

Choice の criteria は **最大 255 オプション**。候補がそれを超える場合は `score` 順に切る。

`Noul`（「候補の中に正解が含まれるか」）を同じリクエストに載せるのは、ABR 未収載・入力が住所でない、を検出するため。Jev は全質問を並列評価するので追加レイテンシはほぼゼロ。

### 戻り値

```python
@dataclass(frozen=True, slots=True)
class Decision:
    index: int | None      # 選ばれた候補の添字。None = 該当なし
    probability: float     # 選択肢への確率質量
    confidence: float      # Jev の確信度
    contains_answer: float # Noul の値
```

`rerank.py` は候補リストと `Decision` の対応づけまでを担い、**閾値との比較はしない**。判断は `geocoder.py` が `config.py` の閾値を見て行う。

### 劣化時の挙動

Jev が 429 / 529 / タイムアウトで応答しない場合、**例外を投げずに語彙スコア最上位へフォールバック**し、`resolved=False` と `note` に理由を入れて返す。API サーバとして、外部モデルの不調で 500 を返さない。

リトライは SDK の `RetryPolicy` に任せ、こちらでは重ねない。

---

## 7. オーケストレーション `geocoder.py`

```python
class Geocoder:
    def __init__(
        self,
        index: TownIndex,
        store: NumberStore,
        model: DecisionModel,
        cfg: GeocoderConfig,
    ) -> None: ...

    @classmethod
    def open(cls, data_dir: Path, *, model: DecisionModel | None = None,
             cfg: GeocoderConfig | None = None) -> Geocoder:
        """既定の構成で開く。model 省略時は環境変数から Jev クライアントを作る。"""

    async def geocode(self, query: str) -> GeocodeResult:
        return (await self.geocode_many([query]))[0]

    async def geocode_many(self, queries: Sequence[str]) -> list[GeocodeResult]: ...
```

`geocode_many` の段取り:

```
1. 正規化          textnorm.normalize()                 純関数
2. 候補生成        CandidateFinder.find()               mmap のみ、IO なし
3. 分岐            候補1件 → ファストパス / 複数 → Jev へ
4. Jev 往復 ①     町字確定（Choice + Noul をまとめて1リクエスト）
5. 番号取得        NumberStore.fetch()                  確定した町字だけ、1件1ブロブ
6. 分岐            テールが一意に一致 → ファストパス / それ以外 → Jev へ
7. Jev 往復 ②     番号確定（1リクエスト）
8. 組み立て        confidence ゲートを見て粒度を決める
```

**ファストパスが全件で効けば Jev 往復は 0 回。** 定型入力が多い実運用ではここが支配的になる。`cfg.always_rerank` で無効化し、Jev 経路の精度を評価できるようにする。

### confidence ゲート

`confidence` が閾値未満のとき**結果を捨てず、粒度を1段上げて返す**。

```
地番が決まらない        → 町字の代表点、level=MACHIAZA
町字が決まらない        → 市区町村の代表点、level=CITY
市区町村が決まらない    → 都道府県の代表点、level=PREF
何も当たらない          → level=UNKNOWN
```

`GeocodeResult.resolved` は「要求された粒度まで確信を持って解決できたか」を表し、`level` が「実際にどこまで解決したか」を表す。この2つを分けることで、呼び出し側が「町字まででよい」用途にそのまま使える。

---

## 8. 構築 `index/build.py`

地番まで入れると 1,887 ファイルの取り込みになるため、**再開可能であることが要件**。

```python
async def build(
    data_dir: Path,
    level: BuildLevel,               # MACHIAZA | RSDT | PARCEL
    *,
    pref: Sequence[str] | None = None,
    city: Sequence[str] | None = None,
    concurrency: int = 4,
    progress: ProgressSink | None = None,
) -> BuildReport: ...
```

`abr.db` の `meta` テーブルに、取り込み済みソースファイルの URL と `Last-Modified` を記録する。再実行時は

- 未取り込み、または `Last-Modified` が変わったファイルだけを処理する
- これがそのまま**差分更新**になる（ABR は定期更新されるため）

層1 (`town.marisa` と `town` テーブル) は全国一括ファイル1本から作るので、毎回作り直す（4 秒）。**一時ファイルに書いてから atomic rename する**ので、サーバが読んでいる最中に構築しても壊れない。

### CLI

```
jev-abr-geocoder build --level {machiaza,rsdt,parcel} [--pref 31] [--city 423912]
jev-abr-geocoder normalize "鳥取市面かげ1-2-3"          # 1件、人間向け出力
jev-abr-geocoder normalize --jsonl < addresses.txt      # バッチ、JSONL 出力
jev-abr-geocoder info                                   # 索引の版・件数・取得日時
```

`normalize --jsonl` は標準入力を `cfg.batch_size` 件ずつまとめて `geocode_many` に渡す。**CLI が制約1 を体現する場所**で、1行ずつ `geocode()` を呼ぶ実装にはしない。

---

## 9. `config.py`

```python
@dataclass(frozen=True)
class GeocoderConfig:
    # --- 閾値 ---
    town_confidence: float = 0.70
    number_confidence: float = 0.60
    contains_answer: float = 0.50

    # --- 候補生成 ---
    max_options: int = 255          # Jev Choice のオプション上限
    fallback_limit: int = 60        # 編集距離スキャンで返す上限
    max_edit_distance: int = 2

    # --- 挙動 ---
    always_rerank: bool = False     # ファストパスを無効化（評価用）
    batch_size: int = 64
    model: str = "jev-latest"

# --- Jev に渡す文言。ここ以外に書かない ---
TOWN_INSTRUCTIONS = "..."
NUMBER_INSTRUCTIONS = "..."
CONTAINS_ANSWER_INSTRUCTIONS = "..."
```

閾値の初期値は暫定。§11 の評価セットが揃ってから実測で決める。

---

## 10. テスト方針

| 対象 | 方法 | Jev |
|---|---|---|
| `textnorm` | 表駆動の純関数テスト | 不要 |
| `index/keys` | エイリアス生成規則の表テスト | 不要 |
| `index/numblob` | encode → decode ラウンドトリップ | 不要 |
| `index/store` | 一時 DB に小さなデータを入れて往復 | 不要 |
| `match/candidates` | 鳥取県だけの小さな索引を固定データから構築 | 不要 |
| `match/rerank` | `FakeModel` でリクエスト組み立てと応答解釈を検証 | 差し替え |
| `geocoder` | `FakeModel` で統合。往復が2回で済むことも検証 | 差し替え |

**`DecisionModel` を Protocol にして注入可能にすることが、テスト可能性のすべて。** テストデータは鳥取県 (`pref31`) と佐々町 (`city423912`) の実ファイルを `tests/data/abr/` に置く（合計 2 MB 程度）。

精度そのものの評価は単体テストとは別枠で、[docs/eval.md](eval.md) に定めた評価セットで行う。

`geocode_many` が「入力が何件でも Jev 呼び出しは高々2回」であることは、`FakeModel` の呼び出し回数を数えて**テストで守る**。制約1 は設計意図なので、退行したら落ちるようにする。

---

## 11. 未解決

- **閾値の初期値** — [docs/eval.md](eval.md) の評価セットが揃うまで暫定値。精度-カバレッジ曲線から動作点を選んで確定させる
- **投機的ファンアウト** — 段 (a) の上位数件について段 (b) を先回りで問い、往復を 2 回 → 1 回にする。効果は評価セットができてから判断する
