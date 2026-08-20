# footy-ev 公開サイト設計書

**結論**: 本サイトの資産はモデルではなく、**「試合前に固定され、第三者が再計算できる予測が積み上がった時間」**である。Dixon-Coles は公開文献であり誰でも週末に再現できるが、2026-08 に始まった track record は後から作れない。したがって設計のすべては「時計を早く動かし始めること」と「読者が我々を信じずに済むこと」に従属する。具体的には (a) **公開モノレポ + モデルコード全公開**（模倣リスクは実在しないが、コードと予測が同一 commit にあることが `params_hash` 検証の前提になる）、(b) **GitHub Actions が予測を生成しコミットする**（git の commit 時刻は著者が偽造できるので、それ単体では第三者検証にならない — DESIGN_PHASE2.md §7.5-1 の弱点）、(c) **節ごとの OpenTimestamps を publish 時に打つ**（月次バッチでは試合後になり事前性を証明できない — 同 §7.5-3 の欠陥）、(d) **The Odds API の生 JSON は公開しない**（規約が standalone data product としての再配布を明示的に禁止。SHA-256 と派生確率のみ公開する — 同 §7.5-2 の修正）。

そして本設計の調査で運用上の締切がひとつ確定した。**2026-27 シーズンは既に第2節まで消化済み（8/7-9, 8/14-15）で、第3節は本週末である。** MVP を「静的 HTML 1枚 + 予測 JSON + OTS スタンプ」まで削り、今週から公開を始める。デザインは後から足せるが、第3節の事前予測は今週しか作れない。

---

## 0. 本設計のために確認した事実

| # | 項目 | 実測 / 調査結果 | 出所 |
|---|---|---|---|
| 0.1 | **J1 確認 run の判定** | **PASS**。`d_RPS +0.0044` 95%CI [+0.0026, +0.0060]、`gap_closed 0.743`、n=3,882（2014-2025）、CAL-1〜4 すべて ok | `reports/backtest_20260820_175749_jpn1_dc_cal.md` |
| 0.2 | 同 run の水準 | model 0.2211 / market(Pinnacle close, Shin) 0.2167 / climatology 0.2337 | 同上 |
| 0.3 | 同 run の Murphy 分解 | Brier 差 +0.00306 のうち **較正不良 -0.00001 / 解像度不足 +0.00290** | 同上 |
| 0.4 | 引き分け較正 | 平均 p_draw 0.2501 / 実測 0.2499 / 差 **+0.0003** | 同上 |
| 0.5 | OOS-LEAGUES run | STRONG, gap_closed 0.773, n=67,291（15ディビジョン） | `reports/backtest_20260820_175626_oos_dc_cal.md` |
| 0.6 | **2026-27 の進行状況** | 第1節 2026-08-07〜09、第2節 2026-08-14〜15 が消化済み（20行）。20クラブ。**第3節は 2026-08-21〜23 の見込み** | `data/raw/JPN_new.csv` 末尾 |
| 0.7 | 2026-27 のベンチマーク列 | **PSCH/PSCD/PSCA は全行空。BFECH/BFECD/BFECA は 20/20 行あり** | 同上 |
| 0.8 | 2026-27 の昇格3クラブ | **Mito / Chiba / V-Varen Nagasaki**（DESIGN_PHASE2.md §6.6 の警告対象） | 同上 |
| 0.9 | 1 fit の実測所要 | mean 0.0071 秒 / max 0.0234 秒、821 fold で合計 5.8 秒 | 0.1 の run |
| 0.10 | **The Odds API 規約** | "Do not resell, repackage, or redistribute our data as a standalone data product"。一方 "use of our data in websites, mobile apps, dashboards, analytical tools ... including commercial use" は許可 | https://the-odds-api.com/terms-and-conditions.html |
| 0.11 | football-data.co.uk の利用条件 | **明示的な利用規約が存在しない。** disclaimer.php は賭博損失の免責のみで、再配布・商用利用・著作権への言及なし | https://www.football-data.co.uk/notes.txt / disclaimer.php |
| 0.12 | **AdSense のギャンブル定義** | ギャンブルコンテンツの定義に **"tips, odds, handicapping" が明記**。表示にはパブリッシャーのオプトイン + **パブリッシャー所在国と閲覧者所在国の双方が承認国** + 閲覧者が成人、が必要。日本・米・英はいずれも承認国 | https://support.google.com/adspolicy/answer/15132179 |
| 0.13 | GitHub Pages 無料枠 | 帯域ソフト上限 100GB/月、サイト 1GB、ビルド 10回/時、デプロイ 10分。**「オンラインビジネス・EC 等の商用サイトの無料ホスティング」は禁止**（広告掲載型情報サイトの可否は条文上グレー、未確認） | https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits |
| 0.14 | Cloudflare Pages 無料枠 | 帯域上限の明記なし、ファイル 2万・単一 25MiB、**500 ビルド/月**、180 ビルド分/月、タイムアウト 20分、カスタムドメイン 100個。商用利用禁止条項は見当たらず | https://developers.cloudflare.com/pages/platform/limits/ |
| 0.15 | J.League の権利管理 | ロゴ・エンブレム・フラッグ・マスコット等について商標権・著作権を包括管理と公式に明記。**「J1」単体の登録有無は一次DB未確認** | https://aboutj.jleague.jp/corporate/activities/various_rights/ |
| 0.16 | OpenTimestamps | 無料・登録不要。パブリックカレンダー（alice/bob 等）稼働中。`pip install opentimestamps-client` → `ots stamp` / `ots upgrade` / `ots verify`。Actions からは通常の CLI 呼び出しで動く（公式の Actions 対応明言はなし） | https://opentimestamps.org/ |
| 0.17 | 現状のリポジトリ | git remote **未設定**、commit 3本。`predictions/`・`footy/pipeline/weekly.py`・`predict` サブコマンドは**未実装** | `git remote -v` / `footy/cli.py` |
| 0.18 | 現状の .gitignore | `data/odds_snapshots/` を「not ours to redistribute」として除外済み。`reports/` も除外 | `.gitignore` |

**0.6 が工程表を、0.10 と 0.12 が §3 と §6 を、0.1〜0.4 がトップページの文言を決める。**

---

## 1. サイト構成と情報設計

### 1.1 設計原則（3つだけ）

1. **サイト上のすべての数字は、それが出てきた成果物へのリンクを持つ。** 「較正されています」ではなく「較正カーブ → その元になった JSON → それを再計算するスクリプト」。誠実さは文体ではなく構造で表現する。
2. **バックテストと前向き記録を絶対に同じ表に入れない**（DESIGN_PHASE2.md §7.4）。トップページでも2つの箱に分ける。混ぜた瞬間にこのサイトの主張は死ぬ。
3. **価格（オッズ）は読者向け画面に一切出さない。確率だけを出す。** 理由は2つあり、両方が独立に効く — (a) ティップスターとの線引き（§6.1）、(b) AdSense の「gambling content = tips, odds, handicapping」定義（0.12）から外れる唯一の余地。データ層（JSON）には再現性のため生オッズを残すが、表示層には出さない。

### 1.2 ページ種別と URL 設計

| # | ページ | URL | 生成 | 更新 | 役割 |
|---|---|---|---|---|---|
| P1 | トップ | `/` | 自動 | 週次 | 10秒で誠実さを伝える。§1.3 |
| P2 | 節ページ | `/j1/2026-27/round-03/` | 自動 | 予測時 + 結果確定時に**追記** | 週次コンテンツの本体。§1.4 |
| P3 | 節インデックス | `/j1/2026-27/` | 自動 | 週次 | シーズン内の全節・順位表 |
| P4 | **Track record** | `/record/` | 自動 | 週次 | 常設ダッシュボード。§1.5 |
| P5 | **Verify** | `/verify/` | 半自動 | 稀 | 第三者検証の手順。§3 |
| P6 | Methodology | `/method/` | 手動 + 自動埋め込み | 稀 | モデル・事前登録・合格条件 |
| P7 | **What we can't see** | `/limitations/` | 手動 + 自動 | 月次 | 既知の弱点。§1.6 |
| P8 | 外れの事後分析 | `/misses/<season>-r<NN>/` | 半自動 | 週次 | DESIGN_PHASE2.md §7.6 |
| P9 | チームページ | `/team/kashima-antlers/` | 自動 | 週次 | 強度時系列。長尾・被引用面 |
| P10 | シーズン予測 | `/season/2026-27/` | 自動 | 週次 | 優勝/ACL/残留確率。最も共有される |
| P11 | データ | `/data/` | 自動 | 週次 | 全 JSON/CSV の索引 + ライセンス。§5.2 |
| P12 | About / 免責 | `/about/`, `/disclaimer/`, `/privacy/` | 手動 | 稀 | §6 |
| P13 | フィード | `/feed.xml`, `/llms.txt`, `/sitemap.xml` | 自動 | 週次 | §5 |

**URL 規則:**
- 節番号はゼロ埋め2桁（`round-03`）。シーズンは 2026-27 以降は `2026-27`、2012-2025 は `2014` のように暦年単一（DESIGN_PHASE2.md §6.4 の `season_of_j1` に一致させる）。
- **URL は一度公開したら変えない。** 節ページに結果を追記しても URL は同じ。これが「後から書き換えていない」ことを読者が確認できる前提になる（Wayback 差分が意味を持つ）。
- チームスラグは `footy/data/teams_j1.py` の正規名から機械生成し、**エイリアス表に固定してリダイレクトを持たない**（`Yokohama F. Marinos` と `Yokohama FC` の衝突事故を URL 層でも起こさないため）。

### 1.3 トップページ — 10秒で誠実さを伝える構成

**メカニズム**: 初見の訪問者に「ティップスターではない」を伝える最短経路は、説明ではなく **(1) 自分に不利な数字を先に出す、(2) 検証リンクを添える、(3) 無いものを列挙する** の3点セットである。ティップスターはこの3つを物理的に書けない。ファーストビューにこれだけを置き、他は全部スクロール下。

```
┌──────────────────────────────────────────────────────────────┐
│  <SITE>                          Record  Method  Verify  Data │
├──────────────────────────────────────────────────────────────┤
│                                                               │
│  Match probabilities for Japan's J1 League.                   │  ← h1
│  Published before kickoff. Scored afterwards. Every time.     │
│                                                               │
│  ┌── BACKTEST 2014-2025 · 3,882 matches ──────────────────┐  │  ← 箱1
│  │  Ranked probability score (lower is better)             │  │
│  │    Pinnacle closing market   0.2167   ██████            │  │
│  │    This model                0.2211   ███████           │  │
│  │    Knowing nothing           0.2337   ██████████        │  │
│  │                                                          │  │
│  │  The betting market is still better than us,            │  │  ← 見出しは自分の負け
│  │  by 0.0044 RPS [95% CI 0.0026-0.0060].                  │  │
│  │  We close 74% of the gap between guessing and the       │  │
│  │  sharpest price in the world.  → how we measure this    │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                               │
│  ┌── LIVE RECORD since 2026-08-21 · 10 matches ───────────┐  │  ← 箱2（別集計）
│  │  Too few matches to mean anything yet. Shown anyway.    │  │
│  │  d_RPS +0.019 [95% CI -0.031 to +0.070]   → full record │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                               │
│  When we say 30%, it happens 30.0% of the time.               │  ← 較正1行 + スパークライン
│  [────calibration sparkline, 45x140px────]     → full curve   │
│                                                               │
│  ✓ Every prediction is timestamped before kickoff  → verify   │  ← 検証
│  ✓ Model code, data and scoring script are public  → GitHub   │
│  ✗ No bookmaker links.  ✗ No tips.  ✗ No affiliate revenue.   │  ← 無いものの列挙
│                                                               │
├──────────────────────────────────────────────────────────────┤
│  Round 3 · 21-23 Aug   [10 matches, published 20 Aug 12:00Z]  │  ← 以下スクロール
```

**厳守事項:**
- 箱1と箱2は**視覚的に別のカード**で、間に合計や平均を置かない。箱2の n が 200 を超えるまで「Too few matches to mean anything yet.」を必ず表示する（閾値を事前登録し、`site/config.py` に定数で持つ）。
- 「74% of the gap」は `gap_closed 0.743`（0.1）。ただし **J1 では gap_closed を判定に使わない**（DESIGN_PHASE2.md §7.3）ので、トップの見出し数値は `d_RPS` を主・`gap_closed` を副として書く。上の文面はその順序になっている。
- **勝率・的中率・ROI・資金曲線・「今週の狙い目」は一切置かない**（DESIGN.md §3 の「明示的に基準に含めないもの」の表示層への延長）。
- ヒーローに画像を置かない。LCP はテキストにする。

### 1.4 節ページ（P2）— 週次の本体

1ページが2フェーズを持つ。**予測ブロックは不変・結果ブロックは追記**、という構造そのものが誠実さの主張になる。

```
[FROZEN BLOCK]  ← predictions/j1/2026-27/round-03.json からのみレンダリング
  published_at (UTC) / model_version / params_hash / file sha256
  → verify links: [GitHub Actions run] [.ots] [Wayback snapshot] [X post]
  10 fixtures × { H / D / A 確率, 期待得点 λ:μ, 市場確率, 乖離 }
  Top-3 divergence（市場と最も食い違う試合）

[APPENDED BLOCK]  ← 結果確定後に追記。追記日時を明記
  結果 / 各試合の RPS / モデル vs 市場 vs 気候値 の節合計
  Worst 3 misses → /misses/ へ
```

**設計上の要点:**
- 予測ブロックのレンダリング元は**必ず凍結 JSON のみ**。DB や parquet から再取得してはならない。凍結 JSON が唯一の真実であることをコードレベルで保証する（`site/render_round.py` は parquet を import しない）。
- 「市場確率」は §1.1-3 のとおり**確率のみ**。オッズ値は出さない。出典表記は毎ページ固定（§6.4）。
- 乖離は `p_model - p_market` の絶対値で並べ、**必ず**「市場のほうが平均して正確である（実測 +0.0044 RPS）。乖離はモデルが正しいという意味ではなく、我々が何を見落としているかの手がかりである」を添える。この一文が §6.1 のティップスター回避の中核。

### 1.5 Track record ダッシュボード（P4）

常設。ナビゲーションの一番左（`/record/`）。**構成順序が重要** — 悪い数字を上に置く。

1. **Live record**（2026-08 以降・BFEC ベンチマーク）: 累積 `d_RPS` と CI、試合数、「n が小さい」注記
2. **Backtest**（2014-2025・PSC ベンチマーク）: 0.1 の判定ブロックをそのまま
3. **較正カーブ**: CAL-2 自前デシル表（判定に使う）を主、市場デシル表を「診断のみ」ラベル付きで従、**期待帯を必ず重ねる**（DESIGN_PHASE2.md §3.2）
4. **Murphy 分解**: 「我々に足りないのは目盛りではなく情報である」を 0.3 の数字で示す
5. **シーズン別内訳**（診断のみラベル）: 2018-19 の `gap_closed -0.41` のような負の年も**必ず載せる**
6. **事前登録の状態**: `params_hash`、pre-registration タグ、閾値表（実行後に変えていないことの表示）

**ベンチマーク世代の境界を必ず可視化する。** 2026 年以降は Pinnacle クローズが消滅し Betfair 取引所クローズ（オーバーラウンド 1.0095 vs 1.0294）に替わっている。取引所クローズはより強いベンチマークなので、**モデルが何も変わっていなくても `d_RPS` は悪化して見える**（DESIGN_PHASE2.md §11-3）。グラフには縦線と注記を入れ、接続較正（同 §6.1）の数値をリンクする。

### 1.6 「What we can't see」（P7）— 最小コストで最大の信頼

戦術知識ゼロで書ける最も強いページ。内容はすべて既存の設計文書から機械的に引ける。

- 見ていないもの: 負傷・出場メンバー・退場・移籍・監督交代・日程過密・移動距離・xG（ライセンス上取得できていない / DESIGN_PHASE2.md §6.2）
- 構造的に弱い時期: **2026-27 開幕数節**（昇格3クラブ Mito/Chiba/Nagasaki を J2 で一切見ておらず、2026年2〜6月の百年構想リーグのデータも無い / 同 §6.6）。**これは事前に宣言し、事後に検証する。**
- 定量的な弱点: Murphy 分解で解像度不足が差の 95%（0.3）
- 各項目に、実際にそれで外した試合（/misses/）へのリンクを貼る

### 1.7 チームページ（P9）とシーズン予測（P10）

- **P9**: 攻撃力 `a_i` / 守備力 `d_i` の時系列、現在のリーグ内順位、直近の予測と結果、次節の予測。**評価文を書かない**（「好調」等は解釈であり戦術論の入口）。「Attack rating: +0.31 (4th of 20), up from +0.18 four weeks ago」までにとどめる。
- **P10**: 優勝・ACL 出場圏・残留の各確率と前週比。**モンテカルロが必要（未実装 / §4.4）。**
  - 残り試合集合は外部フィクスチャ表を取らずに導出できる: 20クラブ総当たり2回戦制なので「全 380 ペア − 消化済み」で厳密に決まる。**openfootball 等への依存を新たに作らない。**
  - 順位決定は 勝点 → 得失点差 → 総得点 まで実装し、それ以降（当該対戦成績・反則ポイント・抽選）は「同値扱いで確率を等分」と明記する。

---

## 2. 技術選定

### 2.1 生成: Python + Jinja2（`footy site build`）

| 案 | 採否 | 理由 |
|---|---|---|
| **Python + Jinja2 + 手書き CSS** | **採用** | 入力が全部 Python オブジェクト（parquet / dict）で、出力は表と折れ線だけ。既存の依存に `jinja2` を1つ足すだけで済む |
| Astro / Eleventy / Hugo | 不採用 | Node ツールチェーンと npm 依存ツリーを丸ごと増やす。得られるのは画像最適化と MDX だが、本サイトに画像は無く、記事は全部生成物。個人運用の保守面が唯一の希少資源 |
| Jupyter → nbconvert | 不採用 | 出力が制御できず、URL 設計と構造化データが入らない |
| 既存の matplotlib PNG をそのまま貼る | 部分採用 | **`savefig(format="svg")` に切り替えて SVG をインライン展開する。** 依存は増えず、拡大に強く、ダークモード対応でき、`<title>/<desc>` でアクセシブルになる。PNG は OGP 画像用にのみ残す |

```
footy/site/__init__.py
footy/site/build.py        # `footy site build` の実体。site/ を全部書き出す
footy/site/render_round.py # 凍結 JSON のみを読む（parquet を import しない）
footy/site/render_record.py
footy/site/charts.py       # matplotlib -> SVG 文字列
footy/site/schema.py       # JSON-LD (Dataset / SportsEvent / Organization)
footy/site/templates/*.html.j2
footy/site/static/style.css   # 手書き ~250行、フレームワークなし
site/                      # 出力（git 管理する。§2.6）
```

- **JS はゼロを既定とする。** 表ソートが欲しくなったら 30 行の inline script のみ許可。外部 CDN を読まない（CSP を厳しくできる、プライバシー的にも clean）。
- 目標: 1ページ 50KB 未満、フォントは system stack、LCP < 1s。

### 2.2 成果物の階層（重要）

```
predictions/j1/<season>/round-<NN>.json     一次・不変・git 管理・CC BY 4.0
        ↓ （追記のみ。結果は result キーに）
public data/*.json, *.csv                   派生・機械可読・git 管理
        ↓
site/**/*.html                              派生・表示専用
```

**HTML は捨てても再生成できるが、`predictions/*.json` は再生成できない。** バックアップ・レビュー・CI の保護対象を JSON に集中させる。

`predictions/j1/2026-27/round-03.json`（スキーマ）:

```jsonc
{
  "schema": 1,
  "league": "jpn1", "season": "2026-27", "round": 3,
  "published_at": "2026-08-20T12:00:00Z",     // 生成時刻（UTC）
  "asof": "2026-08-20T12:00:00Z",             // 学習に使った試合の上限（キックオフ時刻粒度）
  "model_version": "dc-tb-1.0.0",
  "params_hash": "4d322a6fa6ac",              // frozen_params.json のハッシュ
  "code_commit": "9c2a067...",                // このファイルを作った commit（Actions が埋める）
  "phi": {"t": 0.98, "h": 0.01, "d": -0.02},  // 較正層の状態（DESIGN_PHASE2 §4）
  "benchmark": {"source": "the-odds-api", "kind": "pseudo-close",
                "snapshot_sha256": "…", "captured_at": "2026-08-20T11:52:03Z"},
  "fixtures": [{
    "kickoff": "2026-08-22T10:00:00Z",
    "home_id": "jpn1:kashima", "away_id": "jpn1:urawa",
    "home": "Kashima Antlers", "away": "Urawa Reds",
    "p": {"h": 0.463, "d": 0.251, "a": 0.286},
    "lambda": 1.62, "mu": 1.11,
    "market_p": {"h": 0.441, "d": 0.257, "a": 0.302},
    "market_raw": {"h": 2.20, "d": 3.75, "a": 3.20, "book": "pinnacle",
                   "last_update": "2026-08-20T11:48:00Z", "devig": "shin"},
    "result": null                            // 確定後に追記。予測値は絶対に触らない
  }]
}
```

- `market_raw` を JSON に残す理由: 第三者が devig を再計算できないと「市場に対して測っている」という主張が検証不能になる。**1節あたり 30 個の数値であり、The Odds API の "standalone data product" には当たらない**（0.10。生 JSON の丸ごとコミットとは別物 — §2.6）。
- `result` の追記は **JSON パッチではなくフィールド埋め**で行い、`git diff` が `"result": null` → `{...}` の1行だけになるようキー順を固定する。**予測値の行が diff に現れたら CI で落とす**（§3.3）。

### 2.3 ホスティング

| 項目 | GitHub Pages (0.13) | **Cloudflare Pages (0.14)** |
|---|---|---|
| 帯域 | ソフト上限 100GB/月 | 上限の明記なし |
| ビルド | 10回/時（Actions 経由なら非適用） | 500回/月・180分/月 |
| カスタムドメイン | 可 | 可（100個） |
| 商用利用 | **「オンラインビジネス・EC 等」は禁止。広告掲載型情報サイトの可否はグレー（未確認）** | 明記の禁止条項なし |
| アナリティクス | 無し（GA 等を自前で入れる → Cookie 同意バナーが要る） | **Cloudflare Web Analytics が無料・Cookie 不使用 → 同意バナー不要** |
| 将来の拡張 | 静的のみ | Workers / KV へ拡張可 |

**採用: Cloudflare Pages。** 決め手は3つ — (1) 0.13 の商用利用条項が、将来 Patreon リンクや広告を置いたときにグレーになる（規約リスクを設計段階で消しておく）、(2) Cookie を使わないアナリティクスで **EU/UK 読者向けの同意バナーを丸ごと回避できる**（英語圏読者が主対象なので実利が大きく、UI も汚れない）、(3) 帯域の当たり判定が無い。

**冗長化**: 同じ `site/` を GitHub Pages にもミラーで出す（`gh-pages` ブランチ）。コスト 0 で、Cloudflare 障害時とドメイン紛争時の退避先になる。DNS を切り替えるだけ。

**リポジトリと Pages の接続**: Cloudflare Pages は「ビルドコマンドなし・出力ディレクトリ `site/`」の**静的アップロード設定**にする。CF 側で Python を走らせない。ビルドは Actions で行い、CF は push された `site/` を配るだけ。これで CF のビルド分数（180分/月）を一切消費せず、ビルド環境が1つに統一される。

### 2.4 CI: GitHub Actions が週次パイプラインそのものになる

**これが本設計の技術的な要。** 0.9 の実測（1 fit 7ms、821 fold で 5.8 秒）から、**J1 のデータ取得・build・check・fit・予測生成・サイト生成の全工程が Actions の1ランに 2〜3分で収まる**。人手のローカル実行が不要になり、その結果として §3 のタイムスタンプが「著者のマシンの時計」ではなく「GitHub のサーバ時刻」に乗る。DESIGN_PHASE2.md §9 の週次運用表を、そのまま Actions のワークフローに写す。

```
.github/workflows/predict.yml    木 12:00 JST (cron 03:00 UTC)
  fetch(JPN.csv 1req) → build → check → [publish gate] → predict
  → predictions/j1/<season>/round-NN.json を書く
  → ots stamp → site build → commit & push（github-actions[bot] 名義）
  → Wayback Save Page Now を叩く → X/Bluesky に hash を投稿

.github/workflows/reconcile.yml  月 09:00 JST
  fetch → build → check → 結果を result に追記 → 指標再計算 → site build → push

.github/workflows/odds.yml       15分毎 cron、キックオフ起点で発火（DESIGN_PHASE2 §8.4）
  x-requests-remaining をログ。100 未満でデグレード

.github/workflows/verify.yml     全 push
  pytest（リークテスト含む） → verify.py（§3.3） → 予測改竄検出 → gitleaks
```

**publish gate**（DESIGN_PHASE2.md §9 をそのまま。**満たさないときはサイトを更新せずワークフローを失敗させる。空ページや古い予測を出さない**）:
`footy check` 赤 / 学習窓の J1 試合数 < 300 / `team_id` 未解決あり / `params_hash` 不一致 / フィクスチャ数 ≠ 10。

**Secrets**: `ODDS_API_KEY` のみ。`.env` は既に gitignore 済みだが、公開前に **全履歴の secret 監査を必ず行う**（commit 3本なので `git log -p -- .env` と gitleaks で足りる）。万一混入していたら履歴を作り直してから公開する。

### 2.5 リポジトリを公開するか（模倣リスク vs 誠実さの担保）

**結論: モデルコードを含めて公開する。単一の公開モノレポにする。**

検討した4案:

| 案 | 却下理由 |
|---|---|
| 予測 JSON のみ公開・モデル非公開 | **`params_hash` と `code_commit` が検証不能になる。** 「事前に固定した」の証明はできても「事前登録したモデルで作った」の証明ができない。これは主張の半分を失う |
| 公開 repo（予測・サイト）+ 私有 repo（モデル） | 上に同じ。加えて予測ファイルと生成コードが別 commit に散り、監査経路が2本になる |
| 全公開だが frozen_params.json を伏せる | 事前登録の主張と正面から矛盾する |
| **全公開モノレポ** | **採用** |

**模倣リスクの実体を評価した結果、リスクは存在しない:**
- モデルは Dixon-Coles 1997 + 時間減衰 + リッジで、`docs/DESIGN.md` が既に実装水準で記述している。コードを伏せても再現を止められない。
- 奪われて困る収益が無い（アフィリエイト不可・当面無収益）。
- 模倣者が奪えないもの＝**2026-08 に始まった前向き記録**。これは公開の有無と無関係。
- むしろ再現される方が有利。「コードを置いてあるので回してみてください」は、この分野で誰も言えていない主張である。

**公開に伴う実在のリスクは模倣ではなくデータ再配布と鍵漏洩であり、それは §2.6 と §2.4 で処理する。**

ライセンス: コード **MIT**、予測・派生データ **CC BY 4.0**、サイト文章 **CC BY 4.0**。CC BY にするのは §5.2 の被引用戦略と一体（引用の法的経路を先に用意しておく）。

### 2.6 データ境界 — 何をコミットし、何をしないか

| 対象 | 公開 repo | 根拠 |
|---|---|---|
| `footy/**`, `tests/**`, `docs/**` | **する** | §2.5 |
| `data/frozen_params.json` | **する** | 事前登録の要（DESIGN.md §4 で既にそう決めている） |
| `predictions/**/*.json` + `*.ots` | **する** | 成果物そのもの。CC BY 4.0 |
| `site/**`（生成 HTML） | **する** | CF Pages の配信元。差分が「書き換えていない」の証拠にもなる |
| `data/raw/*.csv`（football-data） | **しない** | 0.11 のとおり明示規約が無い。**「規約が無い＝再配布自由」ではない。** DB 権のリスクを取る理由が無く、`footy fetch` で誰でも 1リクエストで取れる。代わりに**取得した CSV の SHA-256 を `data/sources.lock` にコミットする**（我々が見たのと同じファイルであることを第三者が確認できる） |
| `data/odds_snapshots/*.json`（The Odds API 生 JSON） | **絶対にしない** | **0.10 の明示禁止条項に抵触する。** DESIGN_PHASE2.md §7.5-2 の「スナップショットを同時にコミットする」は**この規約と衝突するため実行してはならない** |
| 生 JSON の代替 | **`snapshot_sha256` を予測 JSON に埋める + 使った 3 確率を `market_raw` に残す** | 「公開時点の市場を後から作れない」という §7.5-2 の目的は **hash だけで達成される**（後から違うスナップショットを出しても hash が合わない）。かつ 0.10 の「websites / analytical tools 内での利用」に収まる |
| `data/*.parquet`, `reports/**` | しない（現状維持） | 再生成可能。ただし **判定ブロックを含む Markdown レポートは `docs/reports/` に手動でコミットする**（0.1 の run は公開の根拠なので、成果物として残す） |

**生スナップショットの保管**: ローカル + プライベートなバックアップ（例: 別の private repo か暗号化オブジェクトストレージ）に置く。監査要求があれば個別に開示できる状態にしておき、その方針を `/verify/` に書く。これが規約と検証可能性を両立させる唯一の形。

---

## 3. タイムスタンプの公開検証手段

### 3.1 DESIGN_PHASE2.md §7.5 の評価 — 2点の欠陥

| §7.5 の記述 | 評価 |
|---|---|
| 1. 「予測ファイルを公開 git に push。**commit 時刻は GitHub 側の第三者記録として残る**」 | **誤り、または少なくとも過大評価。** git の commit object の author/committer date は `GIT_COMMITTER_DATE` で任意に設定でき、**著者が自由に偽造できる**。第三者記録なのは GitHub が push を受けた時刻（Events API）であって commit 時刻ではない。しかも公開 Events の保持は短い。**この層は単独では検証手段にならない** |
| 2. 「オッズスナップショットを同時にコミットする」 | **実行不能。** The Odds API 規約に抵触（0.10）。目的は hash で達成できる（§2.6） |
| 3. 「**月1回のバッチ**で OpenTimestamps」 | **目的を満たさない。** 月次だと、月内の全ラウンドの証明が付くのは月末＝**試合が終わった後**になる。「試合前に固定された」の証明にならない。**節ごとに publish 時に打つ**必要がある。OTS は無料・登録不要（0.16）なので、頻度を上げるコストは実質ゼロ |

### 3.2 4層の設計（弱い順・すべて publish 時に同時実行）

```
L0  人が読める     公開ページに published_at を表示し、同時刻に取得した
                  Wayback Machine スナップショットへリンクする
                  → 第三者(Internet Archive)がその時刻に「そのページがそう言っていた」を記録

L1  サーバ証人     予測は GitHub Actions のスケジュール実行内で生成・commit される。
                  commit 者は github-actions[bot]、run の created_at は GitHub のサーバ時刻。
                  ワークフローファイルは公開なので「手作業の余地がない」ことも読める。
                  → 予測ページから当該 run の URL を直リンクする

L2  暗号学的       round-NN.json の SHA-256 に対し publish 時に `ots stamp`。
                  .ots を同じ commit に含め、数時間後の `ots upgrade` で
                  Bitcoin ブロックへの attestation を追記して再コミット。
                  → 誰にも書き換えられない。キックオフ前に確定する

L3  社会的         同時刻に X / Bluesky へ「Round 3 predictions published.
                  sha256:xxxxxxxx …」を投稿。プラットフォーム側の時刻が残る。
                  → 非技術読者に最も伝わり、かつ §5.3 の配信を兼ねる
```

**L1 が新規かつ最重要。** これがあるおかげで L0/L3 が「著者が好きな時に手で押したボタン」ではなくなる。逆に L1 を人手実行に戻すと、他の層の説得力もまとめて落ちる。

**運用規則:**
- `.ots` の upgrade コミットは**予測 JSON のバイト列を変更してはならない**（`.ots` だけを更新する）。CI で強制（§3.3）。
- L2 の attestation が付く前にキックオフした場合でも、L1 と L3 が残る。3層が独立に壊れないことが設計の狙い。
- Wayback の Save Page Now は失敗しうる。**失敗は警告のみで publish を止めない**（L1/L2 が主）。

### 3.3 再計算の公開 — `verify.py`

「誠実さ」の最終形は**読者がダッシュボードを信じずに済むこと**。リポジトリ直下に単体で動くスクリプトを置く。

```sh
python verify.py                 # predictions/**/*.json と公式結果から
                                 # track record・較正・Murphy 分解を再計算し、
                                 # site/data/track_record.json と一致するか assert
python verify.py --timestamps    # 全 .ots を検証し、attestation 時刻 < kickoff を確認
python verify.py --immutability  # git 履歴上、各 round-NN.json の予測フィールドが
                                 # 初回 commit から一度も変わっていないことを確認
```

- **依存は numpy と pandas のみ。`footy` パッケージを import しない。** モデルのバグが検証にも伝播しては意味がない。独立実装であることが価値。
- `verify.yml` ワークフローで毎 push 実行し、**バッジをトップページに出す**。
- `--immutability` は具体的に: 各ファイルの全リビジョンを `git log --follow -p` で辿り、`fixtures[].p`・`lambda`・`mu`・`published_at`・`asof`・`params_hash` のいずれかが変化していたら exit 1。`result` の追記のみを許す。**これは §3.2 のどの層より日常的に効く防御**（うっかりの再生成を止める）。

### 3.4 `/verify/` ページの内容

読者が上から順に実行できるコピペ可能な手順にする。

1. 予測ファイルの hash を自分で計算する（`sha256sum`）
2. `.ots` を検証する（`pip install opentimestamps-client; ots verify round-03.json.ots`）と、Bitcoin ブロック時刻が表示される
3. その時刻が最初のキックオフより前であることを確認する（表で並べて表示済み）
4. `python verify.py` で公開数値を再計算する
5. `./bin/footy backtest --league jpn1 --from 2014 --to 2025` でバックテストを再現する（所要時間の目安つき）
6. 生オッズスナップショットの開示方針（§2.6）と、hash 照合の手順

---

## 4. 週次コンテンツのテンプレート

### 4.1 一覧 — 必要なモデル出力の対応表

| ID | テンプレート | 頻度 | 必要なモデル出力 | 実装状況 | 人手 |
|---|---|---|---|---|---|
| **T1** | Round preview: 予測と市場の乖離 | 週次（木） | `p(H/D/A)`, `λ, μ`, 市場確率（devig 済）, `\|p_model − p_market\|` 順位 | **要 `predict`（未実装）** | 10分 |
| **T2** | Round review: どこで外したか | 週次（月） | 試合別 RPS、モデル/市場/気候値の節合計、寄与上位3試合 | metrics.py 流用 | 20分 |
| **T3** | シーズンシミュレーション | 週次（月） | 現 `θ` からの残り全試合モンテカルロ → 優勝/ACL/残留確率 + 前週比 | **要新規（§4.4）** | 5分 |
| **T4** | 昇格組ウォッチ | 隔週 | 昇格3クラブの `a_i, d_i` 時系列、事前分布 `π` からの移動量、消化試合数 | walkforward の fit 履歴を保存すれば可 | 10分 |
| **T5** | **Expectation Gap 警報** | 隔週 | 直近 N 試合の実得点/実勝点 と モデル期待値（`Σλ`, `Σ p·points`）の乖離上位、**および過去データで測った回帰率** | **要新規（軽い）** | 15分 |
| **T6** | 較正・track record 月報 | 月次 | CAL-1〜4、Murphy 分解、`φ` 時系列、疑似クローズ品質（§8.5） | report.py 流用 | 15分 |
| **T7** | チーム強度ランキング | 週次 | `a_i, d_i` の全チーム順位 + 前週比 | 同上 | 0分（全自動） |
| **T8** | ビッグマッチの数字だけ | 随時 | スコア行列 `M` → 最頻スコア上位、BTTS、Over 2.5、クリーンシート確率 | scoreline.py 流用 | 10分 |

**T1/T2/T3/T7 が毎週の定期便、T4/T5/T6/T8 が埋め草。合計の人手は週 40〜60 分に収まる。**

### 4.2 個別の設計注記

**T1（本体）** — 出力は §1.4 の FROZEN BLOCK そのもの。自動生成文は3文まで:
> "The model's biggest disagreement with the market this round is Kashiwa Reysol vs Chiba: we give Kashiwa 58%, the market 47%. The model has no information the market lacks — it does not see injuries, lineups or travel. Over 3,882 backtested matches the market scored 0.0044 RPS better than this model, so treat a disagreement as a question, not an answer."

最後の1文はテンプレート固定文字列にして、**編集者が消せないようにする**（§6.1）。

**T2** — DESIGN_PHASE2.md §7.6 の定型。「モデルが見ていない情報」欄は運営者が**事実のみ**を埋める固定チェックリストにする:
`退場 / GK負傷 / 主力の出場停止 / 直前の代表招集 / ACL・カップ戦の中2日 / 悪天候による中断`。
**戦術的な解釈を書く欄を作らない**（欄が無ければ書けない）。これが「運営者に戦術知識が無い」を弱点でなく仕様にする方法。

**T3** — 最も共有される。残り試合集合は §1.7 のとおり総当たり差分で厳密に導出。出力は `season_sim/<season>.json` に週次で追記し、**確率の時系列グラフ**を持つ（「9月1日時点で優勝確率18%だった」が後から引用できる = §5.2 の被引用戦略の中核）。

**T5 — 命名に関する重要な注意**: ユーザ要望では「xG 乖離の揺り戻し警報」だが、**本プロジェクトは xG を一切持っていない**（DESIGN_PHASE2.md §6.2、ライセンス上取得していない）。xG を名乗ると事実に反し、サイトの主張そのものを毀損する。名称は **"Expectation Gap"** とし、定義を毎回明記する:
> "This is not xG. We have no shot data. This compares goals actually scored with the goals our Poisson model expected, given both teams' estimated strengths."
さらに、**回帰率を主張するなら実測を添える**: 2014-2025 のデータで「直近5試合の乖離が上位10%だったチームの、次5試合の乖離」を測り、その回帰係数を根拠として毎回引用する。測っていない回帰を「揺り戻し」と呼ばない。

**T7** — 完全自動。人手0分の記事が毎週1本出ることは、更新頻度の下限を保証する意味で運用上の価値が大きい。

### 4.3 文体ガードレール（テンプレートに機械的に埋め込む）

**禁止語**（CI の lint で検出して失敗させる。`site/lint_prose.py`）:
`tip, pick, bet, betting, stake, value bet, edge, ROI, bankroll, lock, banker, sure thing, must-win, deserved, unlucky, tactical, formation, will win, expect X to win`

**必須要素**（各記事テンプレートに固定文字列として埋め込み、編集で消せない）:
- 予測を含むページには「the market is measurably better than this model」の一文
- 全ページのフッタに §6.2 の免責 1行
- 数値には必ず n と CI（または「n が小さい」注記）

**許可される主語**: モデル・市場・確率・観測データ。**禁止される主語**: チームの意思・調子・気持ち。「Kashima are in form」ではなく「Kashima's estimated attack rating has risen 0.13 over four matchweeks」。

### 4.4 未実装で、コンテンツのために必要なモデル出力

| 必要なもの | 用途 | 規模感 |
|---|---|---|
| `footy predict --league jpn1 --round next` | T1・サイト全体の前提 | 中。walkforward の1フォールド分を切り出す |
| フィクスチャ取得 | 同上 | 小。**The Odds API `/events`（0クレジット）が唯一の前向きフィクスチャ源**（JPN.csv は消化済み試合しか持たない）。`teams_j1.py` の明示表で照合し、件数 ≠ 10 で publish gate を落とす |
| 節番号の導出 | URL・見出し | 小。日付クラスタ + 各チーム1回の制約で導出し、`config/j1_rounds.json` で手動上書き可能にする（順延・ミッドウィーク節の事故対策） |
| `θ` 履歴の保存 | T4・T7・P9 | 小。walkforward が既に fit しているので dump するだけ |
| モンテカルロ | T3・P10 | 中。残り試合を `λ, μ, ρ` からスコア生成 → 10,000 反復 → 順位分布 |
| Expectation Gap + 回帰率実測 | T5 | 小〜中 |
| `footy site build` | 全部 | 中。§2.1 |

---

## 5. 流入設計（AI Overviews 時代）

### 5.1 前提: 検索に賭けない

英語圏の「J1 予測」は検索母数が小さく、情報系クエリは AI Overviews に吸われる。**SEO で勝とうとせず、「AI と人間の双方にとっての一次ソース」になることに投資する。** 一次ソースの条件は、他所に存在しない・日付が付いている・機械可読・引用ライセンスが明確、の4つ。本サイトはこの4つを全部満たせる稀な立場にある（英語圏で J1 の較正済み確率を出しているところが無い）。

### 5.2 一次ソース化の具体策

1. **すべての数字を JSON/CSV で同時公開する**（`/data/`）。`predictions/`, `track_record.json`, `team_ratings.json`, `season_sim/<season>.json`。**CC BY 4.0** を明記し、推奨引用形式（BibTeX 含む）を置く。
2. **JSON-LD 構造化データ**:
   - `/data/` と各データセットに **`Dataset`**（`distribution` に実 URL、`license`、`temporalCoverage`、`creator`）— 研究者と AI に最も効く
   - 節ページの各試合に **`SportsEvent`**（`startDate`, `homeTeam`, `awayTeam`, `location`）
   - サイトに `Organization` + `WebSite`
   - Methodology に **`FAQPage` を1つだけ**（濫用しない）
   - **`Review` / `AggregateRating` は使わない**（予測を評価と誤認させる）
3. **`/llms.txt`** を置く。サイト構造・データの場所・引用条件・「賭けの助言ではない」を平文で書く。規格としての普及は未確定だが、コストがほぼ0で、実質「AI 向けの README」として機能する。
4. **安定 URL と日付**。P10 の週次スナップショット（「2026-09-01 時点の優勝確率」）は、後から人にも AI にも引用される唯一の形式。**過去の値を上書きせず、時系列として残す。**
5. **RSS/Atom (`/feed.xml`)**。統計クラスタは実際に RSS で追う。実装 20 行。

### 5.3 検索以外の流入

| 経路 | やり方 | 注意 |
|---|---|---|
| **既存の英語圏 J.League メディア** | ポッドキャスト・ニュースレター・ブログに、**無償・帰属表示のみで数字を提供する**。「今週の優勝確率テーブル、使ってください」 | **最も費用対効果が高い。** 自分で読者を集めるより、既にいる読者に数字を届けるほうが桁違いに速い。CC BY にしてあるので相手の法務も詰まらない |
| **r/JLeague** | 自己宣伝規則を先に読み、**モデレータに事前連絡**して週次スレッドの許可を取る。リンク投下ではなく、**コメント内に数字を書く**形で参加する | 一発 BAN が最大のリスク。数字だけ置いて去るのが最も嫌われる |
| r/soccer, r/soccernerds | 本当に珍しい数字のときだけ（「残留確率が1週間で34%動いた」等） | 頻度を上げない |
| **X / Bluesky** | 週次の Round card 画像 + hash（§3.2 L3 と兼務）。football analytics クラスタは Bluesky に厚い | 自動投稿でよい。返信は人手 |
| **ニュースレター** | Buttondown 等の無料枠。週1、Round preview + Track record 差分 | **自分で保有する唯一の読者名簿。**優先度は高い |
| GitHub | README を作品として書く。Awesome-list への掲載 | 技術者流入 |

**やらないこと**: 有料広告、SEO 記事量産、相互リンク、他サイトへのコメントスパム、Discord の無差別参加。

---

## 6. 法的・規約面のガードレール

### 6.1 ティップスターにならないための設計規則（実装で強制する）

| 規則 | 実装 |
|---|---|
| **オッズ（価格）を表示層に出さない** | `render_*.py` に `market_raw` を渡さない。テンプレートから参照不能にする |
| **推奨・選択を出さない** | 「best bet」「our pick」に相当する UI を作らない。乖離は「順位」ではなく「差」として提示し、色で煽らない |
| **EV・ステーク・ROI・資金曲線を出さない** | DESIGN.md §3 の判定基準に無いものは、表示層にも作らない。`footy/backtest/` を作らないという構造的防御をサイト側にも延長する |
| **ブックメーカーへのリンクを一切張らない** | アフィリエイトに限らず**素のリンクも張らない**。日本居住者としての賭博幇助リスクを 0 に倒す。外部リンク許可リストを CI で検査する |
| **禁止語 lint** | §4.3。CI で失敗させる |
| **「市場のほうが正確」を固定文で必ず併記** | §4.3 の必須要素 |
| **スポンサー禁止** | ブックメーカー・アフィリエイトネットワークからの出稿と提携を受けない旨を `/about/` に明記 |

**この規則群は同時に 0.12（AdSense の gambling = "tips, odds, handicapping"）対策でもある。** オッズも tips も handicapping も出さないサイトである、と構造的に主張できる状態を維持することが、将来の収益化余地を残す唯一の方法。

### 6.2 免責と責任あるギャンブル

**全ページのフッタ（1行、固定）:**
> Statistical model output for research and entertainment. Not betting advice. Not affiliated with the J.League or any club.

**`/disclaimer/`（全文）に含める要素:**
- 予測は確率であり、いかなる結果も保証しない
- 賭けの助言ではなく、賭けを推奨しない。ブックメーカーとの提携・収益関係が一切無い
- 18+（管轄によっては 21+）。賭ける場合は自分の居住地の法に従う責任は読者にある
- **責任あるギャンブルの窓口**: BeGambleAware（英）/ GamCare（英）/ National Council on Problem Gambling 1-800-GAMBLER（米）/ Gambling Help Online（豪）。**読者の主分布に合わせて英・米を必ず入れる**
- モデルの既知の弱点（`/limitations/` へリンク）
- データ出典と、その正確性を保証しないこと

### 6.3 商標 — 「J.LEAGUE」「Jリーグ」「J1」

**調査結果**: J.League はロゴ・エンブレム・マスコット等について商標権・著作権を包括管理していると公式に明言している（0.15）。**「J1」単体の登録有無は一次データベースで未確認**（J-PlatPat が JS フォームのため直接照会できず）。

**この不確実性のもとでのリスク評価と設計判断:**

| 用途 | 判断 |
|---|---|
| **ドメイン名に含める**（`j1predictions.com` 等） | **禁止。** 最も強く出所混同を招く形式で、かつ後から変更するとリンク資産を全部失う。**未確認のリスクを、取り返しがつかない場所に置かない** |
| **サイト名に含める** | **禁止。** 同上 |
| 本文中に記述的に使う（"predictions for Japan's J1 League"） | **可。** クラブ名・リーグ名を指示的に使うのは記述的使用の範囲。ただし `J.LEAGUE` の正式表記・ロゴ体は使わない |
| **ロゴ・エンブレム・マスコット・クラブ紋章の使用** | **禁止（例外なし）。** 0.15 が明示的に管理を宣言している対象そのもの。favicon・OGP 画像・チームページのアイコンにも使わない |
| クラブカラーをブランドカラーに使う | 避ける。中立色にする |
| 「非提携」表示 | **全ページのフッタに明記**（§6.2）。混同のおそれを下げる最も安いコスト |

**サイト名の選定基準**（名称は未定なので `<SITE>` / `<DOMAIN>` をプレースホルダとして使用）:
1. リーグ・クラブの商標語を含まない（J.LEAGUE / Jリーグ / J1 / JFA / クラブ名）
2. 賭博語彙を含まない（bet / odds / tips / picks / value / EV）— §6.1 と 0.12 の両方に効く
3. 手法を示す語を軸にする（calibration / probability / forecast / prior 等）
4. `.com` または `.football` / `.dev`。国別 TLD は避ける

候補（あくまで例）: `calibrated.football` / `probablyfootball.com` / `the-prior.com`。**決定前に J-PlatPat・USPTO TESS・EUIPO eSearch で当該語を実査すること**（本設計では未実施）。

### 6.4 データソースの規約

| ソース | 規約状況 | 本サイトでの扱い |
|---|---|---|
| football-data.co.uk | **明示的な利用規約が無い**（0.11）。免責のみ | 生 CSV を再配布しない（§2.6）。全ページのデータ出典表記に「Historical results and closing prices: football-data.co.uk」とリンク付きで明記。バルクダウンロードを提供しない |
| The Odds API | **生データの standalone 再配布は明示的に禁止。websites / analytical tools 内での利用は明示的に許可**（0.10） | 生 JSON を公開しない。派生確率と hash のみ。表示層ではオッズを出さない（§6.1 と自動的に一致）。出典表記「Pre-match prices via The Odds API」 |
| FBref / J.League 公式 | 使用しない（DESIGN_PHASE2.md §6.2） | 参照もスクレイプもしない。**方針を `/method/` に明記する**（なぜ xG が無いのかの説明として、正直さの材料になる） |

出典表記は**フッタの固定コンポーネント**として全ページに出す。1箇所の変更で全ページに反映される構造にする。

### 6.5 収益化の順序

| 段階 | 手段 | 判断 |
|---|---|---|
| 0（当面） | **無収益** | 方針どおり。信頼を先に積む |
| 1 | **Patreon / GitHub Sponsors** | 最も摩擦が少ない。読者ではなくプロジェクトを支援する形。規約リスクほぼ0 |
| 2 | **有料ニュースレター（Ghost / Buttondown）** | 無料版と有料版の線引きは「速さ・深さ」であって「予測の隠蔽」にしてはならない。**予測本体を有料にした瞬間、track record の公開検証性が壊れる**。予測は常に無料。有料は解説・シミュレーション・API |
| 3 | 広告 | **AdSense は最後にする。** 0.12 のとおり "tips, odds, handicapping" は gambling content に該当し、掲載にはオプトイン + パブリッシャー(日本)と閲覧者の双方が承認国であることが必要。承認国外の読者には非表示になる。§6.1 を守っている限り「該当しない」と主張する余地はあるが、それは Google の分類器に賭けることになる。**先に Ethical Ads / Carbon Ads のような文脈依存・非トラッキング型を検討する**（読者層と親和性が高く、Cookie 同意も不要のままにできる） |
| — | ブックメーカー系アフィリエイト・出稿 | **恒久的に不可**（前提のとおり）。方針として `/about/` に明記し、差別化要素として使う |

---

## 7. 立ち上げ順序（工程表）

**制約**: 0.6 のとおり第3節は本週末。**第3節の事前予測は今週しか作れない。** したがって W0 は「サイトを作る」ではなく「時計を動かす」。

### W0（今週・〜第3節キックオフ前）— MVP：時計を動かす

到達目標: **1つの節の予測が、第三者検証可能な形で、キックオフ前に公開されている。**

| 順 | 作業 | 完了条件 |
|---|---|---|
| 1 | `.env` を含む全履歴の secret 監査 → GitHub に **public** repo を作成し push | gitleaks 緑。`data/odds_snapshots/` と `data/raw/` が履歴に無い |
| 2 | `footy predict --league jpn1 --round next` を実装（フィクスチャは The Odds API `/events`） | `predictions/j1/2026-27/round-03.json` が §2.2 のスキーマで出る |
| 3 | `ots stamp` を手動で1回 | `round-03.json.ots` が commit 済み |
| 4 | **HTML 1枚**（節ページのみ、CSS 50行）を手書きし Cloudflare Pages にデプロイ | 予測・published_at・hash・免責1行・出典表記が載っている |
| 5 | Wayback Save Page Now を手動で叩く / X に hash を投稿 | スナップショット URL がページからリンクされている |

**W0 でやらないこと**: トップページ、track record、methodology、デザイン、ドメイン購入（サブドメインの `*.pages.dev` で始める）。**ドメインは名前が決まってから買う（§6.3 の実査後）。**

### W1（第4節）— 自動化して人手を外す

| 作業 | 理由 |
|---|---|
| `predict.yml` / `reconcile.yml` を Actions 化（§2.4）、publish gate 実装 | **§3.2 L1 の成立条件。ここを越えるまでタイムスタンプの主張は弱い** |
| `footy site build`（Jinja2）＋ 節ページ・節インデックスの生成 | 手書き HTML を捨てる |
| 結果の追記フロー（`result` 埋め）と immutability チェック | §3.3 |
| `/disclaimer/`, `/about/`, フッタ固定コンポーネント | §6.2, §6.4。**公開している以上、早いほうがよい** |

### W2-W3 — 誠実さの常設化

| 作業 | 依存 |
|---|---|
| **`/record/`（P4）**: backtest 箱 + live 箱 + 較正カーブ + Murphy 分解 | report.py 流用 |
| **`/verify/`（P5）と `verify.py`（§3.3）** | 独立実装 |
| **トップページ（§1.3）** | `/record/` の数字 |
| `/method/`（P6）: DESIGN.md / DESIGN_PHASE2.md の要約 + 事前登録 + `params_hash` | 手書き |
| `/limitations/`（P7） | 手書き（材料は既にある） |
| `ots upgrade` の自動化、OTS 検証のバッジ | — |

**ここまでで「誠実さが商品」の主張が構造として完成する。** 以降は流入と厚みの問題。

### W4-W6 — コンテンツを厚くする

| 作業 | 対応テンプレート |
|---|---|
| `θ` 履歴の保存 → チームページ（P9）・強度ランキング（T7） | T7 が人手0分の週次記事になる |
| **モンテカルロ → `/season/`（P10, T3）** | **最も共有される。優先度高** |
| `/misses/`（P8, T2） | 週次運用に組み込む |
| Expectation Gap（T5）と回帰率の実測 | 名称の誤用に注意（§4.2） |
| RSS / JSON-LD / `/data/` / `/llms.txt`（§5.2） | 被引用の準備 |

### W7-W8 — 配信

| 作業 |
|---|
| ドメイン取得（商標実査後）＋ サイト名確定 |
| ニュースレター開設、週次配信を Actions に組み込み |
| r/JLeague モデレータへ事前連絡 → 週次スレッド運用開始 |
| 英語圏 J.League メディアへの数字提供オファー（§5.3、最優先） |
| Bluesky / X の週次自動投稿（§3.2 L3 と兼務） |

### 常設のゲート（毎週）

`footy check` 緑 / publish gate 通過 / `verify.py` 緑 / 禁止語 lint 緑 / immutability チェック緑。**いずれかが赤なら、サイトを更新せずワークフローを失敗させる。** 古い予測や欠けたページを出すことは、1週休むことより高くつく。

---

## 8. 注意点

1. **前向き記録は最初の半年、統計的に何も言えない。** J1 の span は 0.0165 しかなく（DESIGN_PHASE2.md §7.2）、n=100 でペア差の CI 半幅は ±0.005 前後、すなわち PASS 閾値 0.0066 を丸ごと跨ぐ。**この期間にトップページの数字が悪く見えても、モデルを触ってはならない。** 触った瞬間に事前登録が壊れる。`/record/` の live 箱に「n が閾値未満」の注記を機械的に出す仕組み（§1.3）は、運営者自身を守る装置でもある。
2. **2026-27 開幕数節は、本プロジェクト史上いちばん弱い予測である**（0.8、DESIGN_PHASE2.md §6.6）。昇格3クラブを J2 で一切見ておらず、2026年2〜6月の百年構想リーグのデータも無い。**サイト開設と同時に `/limitations/` でこれを宣言し、後で検証する。**これは弱点の告白ではなく、事前宣言→事後検証という商品の実演になる。
3. **ベンチマークが PSC → BFEC に替わったことで、モデルが同じでも `d_RPS` は悪化して見える**（取引所クローズはオーバーラウンド 1.0095 で Pinnacle の 1.0294 より強い / DESIGN_PHASE2.md §11-3）。**接続較正（同 §6.1）をサイト公開前に測り、`/record/` に常設すること。**測る前にグラフを出すと、原因不明の段差を読者に見せることになる。
4. **The Odds API の疑似クローズは「クローズ」ではない**（T−25min でもオーバーラウンド 1.05 / 同 0.18）。サイト上の呼称は `pre-match market price` などの中立語にし、バックテストの Pinnacle クローズと**同じ表に入れない**（同 §8.5）。
5. **未検証の前提**: (a) 「J1」単体の商標登録の有無（0.15、J-PlatPat 未照会）— ドメイン確定前に必ず実査する。(b) GitHub Pages の「商用利用禁止」条項が広告掲載型情報サイトに及ぶか（0.13、条文上グレー）— Cloudflare Pages を主にした理由のひとつだが、ミラーを置く以上いずれ判断が要る。(c) AdSense のオプトイン手続きと審査の要否（0.12 の細部は未確認）。(d) OpenTimestamps のカレンダーサーバは寄付運営で継続保証が無い（0.16）— L1/L3 を併走させる設計にしてあるのはこのため。(e) The Odds API `/events` が J1 のみを返すか（同 §11-4b）。カップ戦混入は publish gate の件数チェックで検出する。
6. **`.ots` の upgrade コミットで予測 JSON を巻き込まないこと。** 1バイトでも変わると hash が変わり、それまでの全層の証明が無効になる。CI で強制する（§3.3）。
7. **サイトのリニューアルで URL を変えないこと。** 過去の予測ページの URL が変わると、Wayback スナップショットとの対応が切れ、§3.2 L0 が死ぬ。デザイン変更は自由、URL は不変。

---

**不採用:**
Astro / Eleventy / Hugo（Node ツールチェーンを丸ごと増やす対価が、表と折れ線しかない本サイトには無い）／matplotlib PNG のままの掲載（SVG に切り替えれば依存増ゼロで拡大耐性・ダークモード・アクセシビリティが得られる）／GitHub Pages を主ホスティングにする案（「商用サイト」条項がグレーで、将来の Patreon/広告と衝突しうる。ミラーとしては維持）／モデルコード非公開（`params_hash` と `code_commit` の検証経路が消え、主張の半分を失う。模倣リスクは実体が無い）／公開 repo と私有 repo の分割（同上に加え監査経路が2本になる）／The Odds API 生 JSON の公開コミット（0.10 の明示禁止条項に抵触。hash で目的は達成される）／football-data.co.uk の生 CSV の再配布（明示規約が無いことは許諾ではない。`footy fetch` で誰でも取得でき、SHA-256 の公開で同一性は担保できる）／git commit 時刻を単独のタイムスタンプ根拠にする案（`GIT_COMMITTER_DATE` で偽造可能。第三者記録は GitHub の push イベントであって commit 時刻ではない）／OpenTimestamps の月次バッチ（証明が試合後に付き、事前性を示せない。節ごとに publish 時に打つ）／サイト名・ドメインへの「J1」「J.LEAGUE」の使用（商標の実査が未了で、かつ後から取り返しがつかない場所）／クラブ紋章・リーグロゴの使用（0.15 が管理対象と明示）／表示層でのオッズ表示（ティップスター化と AdSense のギャンブル分類の両方を同時に招く）／EV・ROI・的中率・資金曲線の掲載（DESIGN.md §3 の判定基準に無く、表示層に作れば必ず判定基準に逆流する）／ブックメーカーへの素のリンク（アフィリエイトでなくても賭博幇助リスクを取る理由が無い）／予測本体の有料化（track record の公開検証性が壊れ、商品の中核が消える）／「xG 乖離」という名称（xG を保持しておらず事実に反する。Expectation Gap と呼び、定義を毎回明記する）／SEO 記事の量産・キーワード狙いの下層ページ（母数が小さく AI Overviews に吸われる。一次ソース化に投資する）／Google Analytics（Cookie 同意バナーが必要になり、Cloudflare Web Analytics で代替できる）／season fixture の外部取得（総当たり差分で厳密に導出でき、新たな規約判断を増やす理由が無い）／記事テンプレートへの「戦術的所見」欄の設置（欄を作れば埋めたくなる。運営者に戦術知識が無いことは仕様であって弱点ではない）。
