# footy-ev 運用の GitHub Actions 移行 設計書

**結論**: 週次パイプラインの全段をローカル Mac の crontab から GitHub Actions に移す。生オッズスナップショットとスケジュール状態は **Cloudflare R2**（S3 互換・private バケット）に置き、公開リポジトリには一切入れない。これで DESIGN_SITE.md §3.2 の **L1 層（予測は GitHub のサーバ時刻で動く公開ワークフロー内で生成・commit される）が初めて実在する**ようになる。同時に L2（OpenTimestamps）を stub から実呼び出しに換え、`.ots` が予測 JSON と**同一 commit** に入るよう段の順序を `predict → stamp → publish` に直した。

移行によって新たに露出する運用リスクは3つあり、それぞれに機械的な防御を置いた:

| リスク | 防御 |
|---|---|
| Actions の cron は遅延・スキップする。T-25min 点を試合開始後に取ってしまう | `plan_points` の遅延上限 (`MAX_LATENESS`) と先読み待機 (`LOOKAHEAD`/`MAX_WAIT`) |
| 無人実行なので、再実行が公開済み予測を黙って上書きしても誰も気づかない | `step_predict` の `existing_conflict` ガード + CI の `check_prediction_immutability.py` |
| 生オッズが公開 repo に混入したら履歴から消せない | CI の `check_data_boundary.sh`（`git ls-files` ベース。`.gitignore` の効かない `git add -f` も捕まえる） |

---

## 0. 本設計のために確認した事実

| # | 項目 | 実測 / 確認結果 | 出所 |
|---|---|---|---|
| 0.1 | スナップショット1個の大きさ | 平均 **48,017 バイト**（28個で 1,344,476 バイト） | `data/odds_snapshots/` の実測 |
| 0.2 | 収集ペース | 2026-08-20〜23 の4日間（第3節・金土日）で **クラスタ7個 × 5点 = 35点**、実ファイル 28個 | 同上 + `.schedule_state.json` |
| 0.3 | 状態ファイルの大きさ | **1,498 バイト**（36エントリ） | `data/odds_snapshots/.schedule_state.json` |
| 0.4 | 年間の増加見込み | 1節あたり4〜5クラスタ ≈ 22〜25ファイル、4.3節/月 ≈ **5MB/月・約58MB/年** | 0.1×0.2 からの外挿 |
| 0.5 | `predict` の入力 | 次節のフィクスチャ一覧は**オッズスナップショットからしか取れない**（`new/JPN.csv` に未消化試合の行が無い） | `footy/pipeline/predict.py` module docstring |
| 0.6 | 現行 `run_schedule` の上限判定 | `due_points` は「target を過ぎたら発火」だけで**上限が無い**。キックオフ後に走っても t25min を発火し `done` を立てる | `footy/pipeline/odds_schedule.py`（移行前） |
| 0.7 | 現行 `write_prediction` | 既存ファイルを**無条件で上書き**する | 同 `predict.py`（移行前） |
| 0.8 | 現行の段の順序 | `WEEKDAY_STEPS[3] == ("predict", "publish", "stamp")` = **stamp は commit の後** | `footy/pipeline/weekly.py`（移行前） |
| 0.9 | ローカル環境 | Python 3.14.5 / pandas 3.0.5 / numpy 2.5.2。263件のテストが全通過 | `./bin/pytest`, `pip freeze` |
| 0.10 | `check_params_hash` の依存 | `git tag -l phase2-preregistered-*` を読む。**tag が無いとゲートは自動的に無効化される** | `footy/pipeline/predict.py` `preregistered_hash` |
| 0.11 | パイプラインの所要時間 | 1 fit 7ms、821 fold で 5.8 秒。全工程が Actions の1ランに2〜3分 | DESIGN_SITE.md 0.9, 2.4 |

0.6・0.7・0.8・0.10 は**ローカルで人が回している限り実害が出にくく、無人化した瞬間に牙を剥く**類の欠陥である。本設計はそれぞれ §4・§6・§5・§6 で塞ぐ。

---

## 1. 何をどこで動かすか（全体像）

```
                     GitHub Actions (public workflows)
  ┌──────────────────────────────────────────────────────────────┐
  │ odds-collect.yml   */10 min   The Odds API ──┐               │
  │ predict.yml        木 03:00Z                 │               │
  │ results.yml        金土日 14:00Z,16:00Z      │               │
  │ reconcile.yml      月 00:00Z                 │               │
  │ ots-upgrade.yml    毎日 06:00Z               │               │
  │ ci.yml             push / PR                 │               │
  └──────────────────────────────────────────────┼───────────────┘
             │ commit/push                       │ get/put
             ▼                                   ▼
   public repo (nippon-numbers)          Cloudflare R2 (private)
   predictions/**, site/**,              odds_snapshots/*.json
   data/frozen_params.json               state/schedule_state.json
             │
             ▼ wrangler pages deploy
      Cloudflare Pages (nipponnumbers.com)
```

公開 repo には**派生物と凍結パラメータだけ**が入り、生データは R2 にだけ入る。この境界が DESIGN_SITE.md §2.6 そのものであり、CI が毎 push で機械的に検査する（§7）。

---

## 2. 生オッズ・状態ファイルの保管先

### 2.1 要件

1. **公開 repo 不可**（DESIGN_SITE.md §2.6 / The Odds API 規約 0.10）。
2. **Actions から読み書きできる**こと。`predict` はスナップショットを**入力**として必要とする（0.5）ので、read-only のバックアップでは足りない。
3. **状態ファイル（1.5KB）を10分ごとに1回だけ読める**こと。10分 cron で毎回アーカイブ全体を引くのは無駄。
4. 保持期限が無いこと。予測の再現・監査要求への個別開示（§2.6）は年単位で効く必要がある。
5. 秘密情報を増やしすぎないこと。

### 2.2 検討した3案

| 案 | 判定 | 決定的な理由 |
|---|---|---|
| **(a) Cloudflare R2** | **採用** | 要件3を単独で満たす唯一の案。オブジェクト単位で 1.5KB の状態ファイルだけを GET できる。無料枠 10GB に対し年58MB（0.4）で約160年分、**egress 課金が無い**ので `predict` が毎週アーカイブ全体を引いても費用が動かない。Pages と同一プロバイダなのでアカウントも増えない |
| (b) 別の private GitHub リポジトリ | 不採用 | **要件3で落ちる。** git は木単位で、`clone --depth 1` でも作業ツリー全体（年58MB、数年で数百MB）を引く。10分 cron で状態ファイル 1.5KB を読むために毎回それを転送することになる。`--filter=blob:none --sparse` で回避はできるが、10分ごとに走る最も壊れてはいけないジョブの中核を、git の部分クローン挙動という繊細な機構に賭けることになる。push 競合が検出可能（要件外の利点）なのは事実だが、concurrency group で同じ保護は得られる |
| (c) Actions artifacts / cache | 不採用 | **要件4で落ちる。** artifacts は既定90日・最大400日、cache は7日未使用で追い出され、かつ総量10GBで LRU 削除される。「消えても再取得できる」データなら妥当だが、**過去のオッズは The Odds API から二度と取れない**（`footy/odds/ingest.py`: スナップショットは immutable かつ再生成不能）。消える可能性のある場所に置いてよい種類のデータではない |

**R2 の弱点として認識していること**: オブジェクトストアは last-writer-wins で、状態ファイルの read-modify-write に競合検出が無い。(b) なら push が reject されて気づけた。これは §3.2 の concurrency group と `merge_state` の2枚で塞ぐ。

### 2.3 バケットのレイアウト

```
r2://<bucket>/
  odds_snapshots/j1_h2h_eu_<UTC>.json    immutable / append-only
  state/schedule_state.json              可変。ただし更新は1ワークフローに直列化
```

ローカルの `data/odds_snapshots/` をそのまま写した形にしてあり、`footy odds sync` 以外は誰もこの構造を知らない。

---

## 3. `footy odds sync` — R2 との同期

### 3.1 実装方針: boto3 を入れず SigV4 を自前で書く

`footy/odds/r2.py` は R2 の S3 互換 API に対する SigV4 署名と、この計画が使う4呼び出し（GET / PUT / ListObjectsV2 / 署名付きリクエスト組み立て）だけを持つ。約70行。

| 案 | 判定 | 理由 |
|---|---|---|
| **自前 SigV4 + `requests`** | **採用** | 依存が**1つも増えない**（`requests` は既存）。署名器は純関数なので、AWS 公式 SigV4 テストスイートの `get-vanilla` ベクタ（署名 `5fa00fa3…fbf31`）に対して**オフラインで正解合わせができる**。DESIGN.md §4 の「テストはネットワークに触れない」と衝突しない |
| boto3 / botocore | 不採用 | 依存8個の環境に ~15MB の botocore が入る。しかもオフラインで動かすには `moto` という9個目が要る。得られるのは「ライブラリのモック」で、**仕様そのものへの照合より弱い検証** |
| `wrangler r2 object get/put` | 不採用 | Node 起動が1オブジェクトあたり数秒。`predict` が100個のスナップショットを引くのに5分かかる。バルク取得の手段が無い |
| Cloudflare Workers 経由の自前 API | 不採用 | 認証と権限をもう一段自分で持つことになる。R2 のトークンで足りるものを増やす理由が無い |

**リスクとして認識していること**: 署名器の自作はバグれば `SignatureDoesNotMatch` という手がかりの無いエラーになる。だからこそ (i) AWS 公式ベクタで固定、(ii) クエリ文字列は署名と実送信で**同一のエンコーダ**を通す（`requests` に dict を渡すと `quote_plus` で再エンコードされ、`+`/`/`/`=` を含む continuation token で食い違う）という2点をコードとテストの両方に明記した。

### 3.2 状態ファイルの競合対策（2枚）

1. **`concurrency: group: odds-collect, cancel-in-progress: false`**。状態ファイルを読む→収集する→書く、の全体が1ジョブなので、これで read-modify-write が直列化される。GitHub は実行中1本＋待機1本しか保持せず、余分な tick は捨てられる — 冪等なので望ましい挙動。
2. **`merge_state`**（`footy/odds/sync.py`）。それでも分岐した場合に備え、**どちらかが「取得済み」と言っている点は取得済みとして残る**（対応するスナップショットがどこかに存在する以上、再取得は重複にクレジットを払うだけ）。`_last_remaining` は**小さい方（悲観側）**を採る — デグレード規則が予算を過大評価しないため。

### 3.3 push の順序

`push` はスナップショットを先に、状態を**最後に**書く。途中で落ちた場合、R2 は「実際より少なく取得済みだと思っている」状態になる。この向きの誤差は最大1クレジットの重複取得で済むが、逆向き（状態だけ先に進む）はスナップショットの記録を丸ごと失う。

### 3.4 削除機能は無い

`pull`/`push` の2方向のみで、mirror モードを置かなかった。スナップショットはこのプロジェクトで唯一**再生成できない**資産であり、誤操作で消える経路を作らない。

---

## 4. Actions の cron は当てにならない — 2つのガード

`schedule:` トリガは共有プールに積まれ、**数分〜数十分遅れて起動し、負荷時には丸ごとスキップされる**。ローカル crontab の15分グリッドは、そのまま Actions の15分グリッドにはならない。そして遅延の被害を受けるのは、**5点のうち唯一「クローズ」と呼べる粒度の t25min** である。

頻度を上げるだけでは足りない（遅延幅は頻度と独立）。実装したのは次の2つで、どちらも `plan_points` の純関数として `tests/test_odds_schedule.py` で検査している。

### 4.1 遅延上限 `MAX_LATENESS`

| label | 上限 |
|---|---|
| t72h | 24h |
| t24h | 12h |
| t6h | 3h |
| t2h | 1h |
| **t25min** | **20分** |

加えて、**クラスタのキックオフを過ぎたら一切発火しない**（`guard_kickoff`）。

理由: `close.py` は既に `book_last_update >= commence_time` の気配を落とすので、キックオフ後のスナップショットが下流で誤採用されることは無い。しかし**状態ファイルには `t25min: done` が立ってしまい、その節は最良の事前オッズを静かに失う**。「t2h が最も近い読み値だった」と正直に劣化する方が、実際には T+5min の値を `t25min` という名前で記録するより良い。

### 4.2 先読み待機 `LOOKAHEAD` / `MAX_WAIT`

t2h・t25min については、target が**15分以内の未来**なら、その run が sleep して正確な時刻に発火する。cron が数分早く起動した場合に「次の run に任せる（その run も遅れるかもしれない）」を避ける。`MAX_WAIT = 15分`、ジョブの timeout は30分。

cron は `*/10`。10分間隔 × 15分の先読みで、各 target は平均1.5本の run にカバーされる。**public repo なので Actions の実行時間は無料**であり、sleep のコストは concurrency group を最大15分占有することだけ。

### 4.3 クレジット予算への影響

冪等性（`.schedule_state.json`）があるので、cron 頻度を上げてもクレジットは増えない。DESIGN_PHASE2.md §8.4 の見積り（5点 × 4.5クラスタ × 4.3節/月 ≈ 97/月、カップ戦込み150〜200）は変わらない。`results.yml` の `/scores` が 2クレジット × 2回 × 3日 × 4.3週 ≈ 52/月 を追加し、合計は月500枠に対して 250 前後。

---

## 5. L2: OpenTimestamps を実呼び出しにする

### 5.1 段の順序を直した（`predict → stamp → publish`）

移行前は `("predict", "publish", "stamp")` で、**stamp は commit の後に走っていた**（0.8）。DESIGN_SITE.md §3.2-L2 は「`.ots` を**同じ commit に含める**」ことを要求しており、この順序ではどうやっても1コミット遅れる。読者が「attestation はキックオフ前か」を検証するとき、予測が入った commit と `.ots` が入った commit が別物になってしまう。

`WEEKDAY_STEPS[3]` を `("predict", "stamp", "publish")` に変え、`step_publish` は `stamp_result` を受け取ってその成果物（`<round>.ots.json` と、実 stamp が取れていれば `.ots` バイナリ）を同じコミットに載せる。

### 5.2 `ots` は隔離された virtualenv に入れる

`opentimestamps-client` は `python-bitcoinlib` に依存し、こちらは本プロジェクトが固定した Python 3.14 より保守的なバージョン追随をする。**L2 は警告のみの層**（DESIGN_PHASE2.md §9）なので、その依存が予測を計算する環境を壊せてはならない。ワークフローは `setup-footy` より**前**に、ランナーのシステム Python で `$RUNNER_TEMP/otsenv` を作り、パスを `FOOTY_OTS_BIN` で渡す。`continue-on-error: true` と `ots_stamp_round` 自身のフォールバック（例外を出さず `stamped: false` の記録を書く）の二重で、失敗はネットワーク不通と同じ「警告」に落ちる。

### 5.3 `ots upgrade` を日次ワークフローにした

`ots stamp` はカレンダーサーバへのコミットメント提出までで、Bitcoin ブロックに固定されるのは数時間後。`ots-upgrade.yml` が毎日 06:00Z に `footy stamp-upgrade` を回し、**`.ots` だけ**をコミットする。DESIGN_SITE.md §3.2 の運用規則「upgrade コミットは予測 JSON のバイト列を変更してはならない」を、pathspec（`git add -- 'predictions/*.ots'`）と、直前の `git diff --quiet -- 'predictions/*.json'` チェックの二重で強制する。

なお `<round>.ots.json` の `upgraded` フラグは**更新しない**。更新すると JSON が動いてしまい、上の規則と自分で衝突する。attestation の証拠は `.ots` 自身であり、フラグではない。

---

## 6. ワークフロー一覧

| ファイル | 起動条件 (UTC) | JST | 何をするか | 書き込み先 |
|---|---|---|---|---|
| `ci.yml` | push(main) / PR / 手動 | — | `./bin/pytest` + データ境界検査 + 予測不変性検査 | なし |
| `odds-collect.yml` | `*/10 * * * *` | 10分毎 | R2から状態pull → `footy odds schedule` → R2へpush | **R2 のみ** |
| `predict.yml` | `0 3 * * 4` | 木 12:00 | R2からスナップショットpull → fetch/predict/stamp/publish → site build → push → deploy | repo + Pages |
| `results.yml` | `0 14,16 * * 5,6,0` | 金土日 23:00 / 翌01:00 | `footy weekly --steps scores` → site build → push → deploy | repo + Pages |
| `reconcile.yml` | `0 0 * * 1` | 月 09:00 | fetch/reconcile/calibrate/report → site build → push → deploy | repo + Pages |
| `ots-upgrade.yml` | `0 6 * * *` | 毎日 15:00 | `footy stamp-upgrade` → `.ots` のみ commit → push | repo |
| `deploy.yml` | 手動のみ | — | 現在の `site/` を再デプロイ（ロールバック・障害時用） | Pages |

**共有 concurrency group `repo-write`**: main に push する4本（predict / results / reconcile / ots-upgrade）は同じグループに入れてあり、同時に走って push が衝突することがない。

**`deploy.yml` に `on: push` を付けなかった理由**: `GITHUB_TOKEN` で行った push は別のワークフローを起動しない（GitHub のループ防止）。push トリガのデプロイは**人間のコミットでは動き、bot のコミットでは動かない**という、望みと正反対の挙動になる。各ワークフローが自分の出力をその場でデプロイする形にした。

**checkout の `fetch-depth: 0`**: `predict.yml` と `reconcile.yml` は必須。既定の浅いチェックアウトは tag を持って来ず、`check_params_hash` が「preregistration tag が無い → ゲート無効」と判定して**事前登録の保証が黙って外れる**（0.10）。

**publish gate の扱い**: `run_weekly` は失敗段があると非ゼロで終わるので、ゲートに掛かった週はワークフローが赤く落ち、site build もデプロイも走らない。DESIGN_SITE.md §2.4 の「満たさないときはサイトを更新せずワークフローを失敗させる。空ページや古い予測を出さない」がそのまま実装になっている。

### 6.1 無人実行が要求した追加ガード: 予測の上書き禁止

`write_prediction` は既存ファイルを無条件で上書きする（0.7）。人が回している限り気づくが、**Actions の再実行・手動再ディスパッチ・リトライは日常的な事象**であり、そこで公開済み予測が静かに書き換わると DESIGN_SITE.md §3.3 が守ろうとしているものが失われる。

`step_predict` は書き込み前に `existing_conflict` を通す:

- ファイルが無い → 通常どおり書く
- 既にあり、**不変フィールドが一致** → `written: False` の no-op として成功（同日リトライはここに落ちる）
- 既にあり、**不変フィールドが不一致** → **ok: False で停止**。ワークフローは赤くなり、人が判断する

不変フィールドは `round_id / season / model_version / params_hash` と各試合の `event_id / commence_time / p_raw / p_calibrated`。`asof` と `generated_at` は §3.3 の「変えてはならない」リストに入っているが**比較には使わない** — 再実行のたびに必ず動く値なので比較に入れると無害なリトライと本物の改竄を区別できなくなる。両者を守るのは比較ではなく**動作**の側で、公開済みファイルを書き換えないので、公開時の `asof` はそのまま残る。同日再実行はモデルの当てはめまで同一（`asof` は日付に正規化される）なので、確率値が「リトライか改竄か」の正直な判別子になる。

---

## 7. CI の2つのガード

### 7.1 `check_data_boundary.sh`

`git ls-files` で、`data/odds_snapshots/*`・`data/raw/*`・`.env`・`.env.*` が**追跡されていない**ことを確認する。`.gitignore` は第一の防御だが `git add -f` を止められないし、一度 push された生データは履歴・全クローン・GitHub のフォークキャッシュから消せない。「後のコミットで削除すれば良い」が効かない唯一の規則なので、機械で見張る。

### 7.2 `check_prediction_immutability.py`

push / PR の差分で `predictions/j1_*.json` が**変更**されている場合、親リビジョンと不変フィールドを比較する。`result` の追記だけが許され、確率値・フィクスチャ・round の同一性が動いていたら落とす。削除も落とす。

**`footy` を import しない**（DESIGN_SITE.md §3.3 が `verify.py` に課したのと同じ独立性）。モデルのコードのバグが、そのコードの産物への変更を素通しできてはならない。独立性の代償としてフィールド一覧が二重化するので、`tests/test_ci_guards.py` が両方を import して**一致を assert する**（`import footy` が混入していないことも含めて）。

**このガードが効く範囲は「人間の push と PR」だけである。** `GITHUB_TOKEN` で行われた push は別のワークフローを起動しないので（§6 と同じ仕組み）、bot 自身のコミットは `ci.yml` を通らない。パイプライン経路をカバーするのは §6.1 の `existing_conflict` の方で、両者は**別々の攻撃面を担当する二枚**であって、片方が他方の予備ではない。

---

## 8. 移行手順（要約）

具体的な操作手順は **`docs/SETUP_ACTIONS.md`** にステップ形式で書いた。設計上の要点だけ:

1. **R2 バケットと R2 API トークンを先に作る。** トークンは「Object Read & Write」を**そのバケット1つだけ**に絞る。Pages 用の Cloudflare API トークンとは**別物**で、権限も別（Pages トークンは Account → Cloudflare Pages → Edit のみ）。
2. **ローカルから既存の28ファイル＋状態を一度 push する**（`./bin/footy odds sync push`）。これをやる前に Actions を有効にすると、Actions 側は空の状態から始めて既取得の点をもう一度買う。
3. **GitHub Secrets を6つ登録する**（§8.1）。値は**コードにもログにも絶対に書かない**。
4. **ローカル cron を止めるのは、Secrets 登録の直後・`odds-collect.yml` を手動ディスパッチする直前**。両方走っている間、ローカル cron は R2 を知らないので状態が分岐し、同じ点に二重にクレジットを払う。逆に、先に止めて Secrets が未設定だと収集に穴が空く。**順序は「R2へpush → Secrets登録 → cron停止 → 手動ディスパッチで確認」**。
5. `predict.yml` は初回だけ `dry_run: true` で手動ディスパッチし、commit/push/deploy 無しで通ることを見る。

### 8.1 必要な Secrets

| 名前 | 用途 | どこで作るか |
|---|---|---|
| `ODDS_API_KEY` | The Odds API | 既存（`.env` にあるもの） |
| `CLOUDFLARE_ACCOUNT_ID` | Pages デプロイ先 / R2 エンドポイントの導出 | Cloudflare ダッシュボード（秘密ではないが Secrets に置いて統一） |
| `CLOUDFLARE_API_TOKEN` | `wrangler pages deploy` | Cloudflare → My Profile → API Tokens |
| `R2_BUCKET` | バケット名 | 自分で決める |
| `R2_ACCESS_KEY_ID` | R2 の S3 API | Cloudflare → R2 → Manage R2 API Tokens |
| `R2_SECRET_ACCESS_KEY` | 同上（**一度しか表示されない**） | 同上 |

`R2_ENDPOINT` と `R2_REGION` は任意の上書き用で、通常は設定しない（アカウントIDから導出、region は `auto`）。

---

## 9. 未解決事項

1. **`verify.py` が無い。** DESIGN_SITE.md §3.3 は `footy` に依存しない独立再計算スクリプト（`--timestamps` / `--immutability` 付き）を要求している。今回入れたのは CI ガードとしての `--immutability` 相当だけで、track record・較正・Murphy 分解の独立再計算と、`/verify/` ページ、トップのバッジは未着手。
2. **L0（Wayback Save Page Now）と L3（X / Bluesky 投稿）が未実装。** どちらも publish 時に1リクエストで済む。L3 は `x-poster` の担当範囲と重なるので、文面の扱いを決めてから。
3. **サードパーティ Action を tag で参照している**（`actions/checkout@v4`, `actions/setup-python@v5`, `cloudflare/wrangler-action@v3`）。供給網の厳密さを求めるならコミット SHA へのピン止めが望ましい。
4. **Cloudflare API トークンの最小権限は未実測。** `wrangler pages deploy` は Account → Cloudflare Pages → Edit で通る想定だが、`wrangler` のバージョンによって User → Memberships → Read を要求する報告がある。実際に落ちたら SETUP_ACTIONS.md §3 に追記する。
5. **`opentimestamps-client` が Python 3.14 系ランナーの system python で入るかは未検証**（`python-bitcoinlib` 次第）。落ちても L2 は stub に劣化するだけで publish は止まらないが、実際に stamp が取れているかは初回の run ログで確認が要る。
6. **GitHub は60日間リポジトリに活動が無いと scheduled workflow を無効化する。** 週次で commit が入る間は問題にならないが、オフシーズンに止まる可能性がある。
7. **予測の再現性は `requirements.lock` に賭けている。** 同じ lock・同じ Python 3.14 なら同じ数字が出るはずだが、BLAS の実装差（macOS Accelerate vs Linux OpenBLAS）まで揃えてはいない。DESIGN_SITE.md §3.4 step 5 の「バックテストを再現する」で最下位桁がずれる可能性は残る。
