# GitHub Actions 移行 セットアップ手順

この文書は**あなたが手で行う操作だけ**を順番に並べたものである。設計の根拠は `docs/DESIGN_ACTIONS.md` にある。

所要時間の目安: 30〜40分。うち待ち時間が10分ほど。

> **鍵の扱いについての約束**
> このリポジトリのコード・ワークフロー・ドキュメントのどこにも、API キーやシークレットの**値**は書かれていない。以下の手順でも、値は Cloudflare / GitHub の画面と、あなたのターミナルの中だけを通る。**値をチャットに貼らないこと。** 貼ってしまった鍵は、そのトークンを削除して作り直す。

---

## 全体の流れ

```
STEP 1  R2 バケットを作る                       (Cloudflare)
STEP 2  R2 API トークンを作る                    (Cloudflare)  → 鍵2つ
STEP 3  Cloudflare API トークンを作る            (Cloudflare)  → 鍵1つ
STEP 4  ローカルの既存データを R2 へ一度上げる    (ターミナル)
STEP 5  GitHub Secrets を6つ登録する             (GitHub)
STEP 6  ローカル cron を止める                   (ターミナル)   ← 順序が重要
STEP 7  odds-collect を手動で1回まわして確認      (GitHub)
STEP 8  predict を dry-run で確認                (GitHub)
STEP 9  スケジュールに任せる
```

**STEP 4 → 5 → 6 の順序は動かさないこと。** 理由は STEP 6 に書いた。

---

## STEP 1. R2 バケットを作る

1. Cloudflare ダッシュボード → 左メニュー **R2 Object Storage** を開く。
   - 初回は R2 の有効化（支払い方法の登録）を求められる。**無料枠は 10GB / 月**で、このプロジェクトの使用量は年間 58MB 程度（`DESIGN_ACTIONS.md` 0.4）なので、通常は課金されない。
2. **Create bucket** を押す。
3. **Bucket name**: `nippon-numbers-odds`（好きな名前でよい。あとで `R2_BUCKET` として登録する）
4. **Location**: 自動（Automatic）のままでよい。
5. 作成後、バケットの **Settings** を開き、**Public access が「Not allowed」のまま**であることを確認する。ここが public になると生オッズが公開されてしまう（`DESIGN_SITE.md` §2.6 が絶対に避けろと言っているもの）。

---

## STEP 2. R2 API トークンを作る（鍵2つ）

R2 の S3 互換 API 用の鍵で、**STEP 3 の Cloudflare API トークンとは別物**である。

1. R2 の画面の右側、**{} API** → **Manage API tokens**（古い UI では「Manage R2 API Tokens」）を開く。
2. **Create API token** を押す。
3. 設定:
   - **Token name**: `nippon-numbers-actions`
   - **Permissions**: **Object Read & Write**（Admin を選ばない）
   - **Specify bucket(s)**: **Apply to specific buckets only** を選び、STEP 1 で作ったバケット**1つだけ**を指定する
   - **TTL**: Forever（または運用に合わせて。期限を切るなら更新日をカレンダーに入れる）
4. **Create API Token** を押す。
5. 表示される画面から、次の2つを控える:
   - **Access Key ID**
   - **Secret Access Key** ← **この画面を閉じると二度と表示されない**
   - （同じ画面に出る **Account ID** も STEP 5 で使う。これは R2 画面や URL からいつでも確認できる）

---

## STEP 3. Cloudflare API トークンを作る（鍵1つ・Pages デプロイ用）

1. Cloudflare ダッシュボード右上のアカウントアイコン → **My Profile** → **API Tokens**。
2. **Create Token** → 一番下の **Create Custom Token** の **Get started**。
3. 設定:
   - **Token name**: `nippon-numbers-pages-deploy`
   - **Permissions**: `Account` / `Cloudflare Pages` / **Edit** の1行だけ
   - **Account Resources**: `Include` / あなたのアカウント1つ
   - **Client IP Address Filtering**: 空のまま（Actions の実行 IP は固定できない）
   - **TTL**: 任意
4. **Continue to summary** → **Create Token**。表示された値を控える（**一度しか表示されない**）。

> もし後で `wrangler pages deploy` が権限エラーで落ちたら、同じトークンに `User` / `User Details` / **Read** を足して再試行する（`DESIGN_ACTIONS.md` §9-4 の未解決事項）。

---

## STEP 4. ローカルの既存データを R2 へ一度上げる

まだローカル cron は止めない。ターミナルで:

```sh
cd ~/develop/footy-ev

# 鍵をこのシェルにだけ渡す（履歴に残さないため、行頭に空白を1つ置く）
 export R2_ACCOUNT_ID='...'          # STEP 2 で見た Account ID
 export R2_BUCKET='nippon-numbers-odds'
 export R2_ACCESS_KEY_ID='...'       # STEP 2
 export R2_SECRET_ACCESS_KEY='...'   # STEP 2

# まず何が上がるかだけ見る（通信するが書き込まない）
./bin/footy odds sync push --dry-run

# 問題なければ本番
./bin/footy odds sync push
```

期待する出力（ファイル数は増えている可能性がある）:

```
snapshots: 28 to upload
state: 36 entries -> r2://state/schedule_state.json
push: 28 snapshot(s), state=written
```

確認のため、逆方向が空になることを見ておく:

```sh
./bin/footy odds sync pull --dry-run
# → pull: 0 snapshot(s), state=merged
```

終わったらこのシェルを閉じる（`exit`）。エクスポートした鍵をシェル履歴や他の作業に持ち越さないため。

> `error: R2 is not configured -- missing ...` が出たら、足りない環境変数の名前がそのまま表示される。

---

## STEP 5. GitHub Secrets を6つ登録する

1. https://github.com/tuyuansama-blip/nippon-numbers → **Settings** → **Secrets and variables** → **Actions**。
2. **New repository secret** で以下を1つずつ登録する。**名前は完全一致**させること。

| Name | Value |
|---|---|
| `ODDS_API_KEY` | The Odds API のキー（ローカルの `.env` にあるもの） |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare の Account ID |
| `CLOUDFLARE_API_TOKEN` | STEP 3 のトークン |
| `R2_BUCKET` | STEP 1 のバケット名 |
| `R2_ACCESS_KEY_ID` | STEP 2 の Access Key ID |
| `R2_SECRET_ACCESS_KEY` | STEP 2 の Secret Access Key |

`.env` の中身を確認するなら:

```sh
cd ~/develop/footy-ev && cut -d= -f1 .env      # キー名だけ表示（値は出さない）
```

3. 6つ登録し終えたら、一覧に6行あることを目視で確認する。

---

## STEP 6. ローカル cron を止める

**Secrets の登録が終わった直後、STEP 7 の手動実行の直前**に行う。

- **早すぎると**（STEP 5 の前）、Actions がまだ動けないので収集に穴が空く。
- **遅すぎると**（STEP 7 のあと）、ローカル cron と Actions が**同じ状態ファイルを別々に持つ**。ローカル cron は R2 を知らないので、Actions が取った点をもう一度取りに行き、その分だけ API クレジットを二重に払う。

```sh
crontab -e
```

`collect_odds.sh` の行の先頭に `#` を付けてコメントアウトし、保存する。削除ではなくコメントアウトにしておくと、Actions に問題が出たときに戻せる。

確認:

```sh
crontab -l | grep collect_odds
# → 行頭に # が付いていること
```

---

## STEP 7. odds-collect を手動でまわして確認

1. GitHub → **Actions** タブ。初回は「I understand my workflows, go ahead and enable them」の確認が出るので承認する。
2. 左の一覧から **odds collect** → **Run workflow**。
   - まず **Dry run** に**チェックを入れて**実行する（API クレジットを使わない）。
3. 実行ログを開き、次を確認する:
   - `Pull the schedule state from R2` に `state: 36 entries -> ...` のような行が出ている（= R2 に繋がっている）
   - `Collect the points this run owns` に `N clusters, M point(s) planned` が出ている
4. 問題なければ **Run workflow** をもう一度、今度は **Dry run のチェックを外して**実行する。
5. `Push new snapshots and the state to R2` が成功していることを確認する。

以後は10分ごとに自動で走る。

---

## STEP 8. predict を dry-run で確認

木曜を待たずに確認しておく。

1. Actions → **predict** → **Run workflow** → **Dry run** に**チェックを入れて**実行。
2. 確認するログ:
   - `Pull the odds snapshots from R2` — スナップショットが降りてくる
   - `Install the OpenTimestamps client` — 失敗していても構わない（L2 は警告のみの層）。失敗した場合は黄色の警告になる
   - `fetch -> predict -> stamp -> publish` — `[predict] ok=True` が出る。dry-run なので commit もタグも作られない
   - `Rebuild the site` — `written: .../index.html` が出る
3. `[predict] ok=False` で落ちた場合、理由がそのまま出る。よくあるもの:
   - `publish gate blocked` → `footy check --league jpn1` が赤い、学習窓が300試合未満、`team_id` 未解決、`params_hash` 不一致のいずれか。ログに具体的な理由が列挙される
   - `no upcoming fixtures found` → スナップショットに未来の試合が無い（オフシーズン、または R2 の pull が空）
   - `already published and unchanged` は**正常**（その節は既に公開済み）

---

## STEP 9. スケジュールに任せる

| 曜日・時刻 (JST) | 走るもの |
|---|---|
| 10分ごと | odds collect |
| 木 12:00 | predict（予測生成 → stamp → commit → デプロイ） |
| 金土日 23:00 / 翌 01:00 | results（暫定結果 → デプロイ） |
| 月 09:00 | reconcile（確定結果 → デプロイ） |
| 毎日 15:00 | ots upgrade（`.ots` に Bitcoin attestation を追記） |

**scheduled workflow は遅れる。** GitHub の共有プールで待たされるので、木曜 12:00 の予測が 12:20 になることは普通にある。設計上そう想定してある（`DESIGN_ACTIONS.md` §4）。

---

## 困ったときに見るところ

### 収集が止まっている

- Actions タブで **odds collect** の直近の run が緑か。赤なら開いてどのステップか見る。
- `R2 is not configured -- missing ...` → Secrets の名前の綴り違い。
- `HTTP 403` → R2 トークンの権限がバケットに向いていない（STEP 2 の bucket 指定を見直す）。
- The Odds API のクレジット切れ → run のログに `remaining=` が出ている。100 未満になると t72h/t24h が自動で落ちる（デグレード規則）。

### 60日ルール

GitHub は**リポジトリに60日間まったく活動が無い**と scheduled workflow を自動で無効化し、メールを送ってくる。週次で bot のコミットが入っている間は起きないが、オフシーズンで長く止まったら Actions タブに出る「Enable workflow」を押し直す。

### 元に戻したいとき

1. Actions → 対象のワークフロー → 右上「…」→ **Disable workflow**。
2. `crontab -e` でコメントアウトした行の `#` を外す。
3. R2 のデータは残っているので、ローカルに引き戻すなら `./bin/footy odds sync pull`（STEP 4 と同じ環境変数が要る）。

### 鍵を漏らしてしまったら

1. Cloudflare 側: R2 API トークン / Cloudflare API トークンをその場で **Revoke** し、作り直して STEP 5 をやり直す。
2. The Odds API 側: ダッシュボードでキーを再発行し、`.env` と GitHub Secrets の両方を更新する。
3. リポジトリに commit してしまった場合、**削除コミットでは消えない**。履歴を書き換えて force push し、その上で鍵を作り直す。CI の `data boundary` チェックはこれを未然に止めるためにある。
