# 採点エージェント

ローカルVLM（画像が読めるモデル）で、スキャンした手書き課題を一括採点するツール。

提出物フォルダを走査 → 各課題の最新版をVLMに読ませて採点 → 各学生フォルダと全体集計CSVに結果を保存する。

## 引き継ぐ方へ（最初に読んでください）

このリポジトリは大きく2つの部分に分かれています。**自分の授業に合わせて使う場合、必要なのは前半（コア）だけ**のことが多いです。

- **コア（どの授業でも使う）**: `grader.py` … 提出物を採点してCSV/JSONを出力する本体。
- **この授業固有（任意・参考）**: `teams_autofill.py` / `add_teams_columns.py` … Microsoft Teams（Web）の成績ページへ点数を自動入力するための補助。**Teamsの画面構造・課題の並びに強く依存**しているので、別のLMSや別の課題構成ではそのままでは動きません。仕組みの参考としてください。

サンプルの採点基準や `assignment_config.json` の課題名（`事前課題1` 等）も**この授業のもの**です。自分の授業の課題名・基準に置き換えて使ってください。

> **配布・引き継ぎ時の個人情報チェックは末尾の「引き継ぎ時のチェックリスト」を必ず確認してください。**

---

# コア: `grader.py`

## 前提とするデータの形

### 提出物のフォルダ構造
`grader.py` は次の入れ子を前提にしています（Teamsから課題提出物を一括ダウンロードしたときの構造）。

```
Submitted files/
├── 学生氏名A/
│   └── 事前課題1/              ← 採点対象の課題名フォルダ
│       ├── バージョン 1/
│       │   └── 提出ファイル.pdf
│       └── バージョン 2/       ← 「バージョン N」は数字の大きい方を採点
│           └── 提出ファイル.pdf
└── 学生氏名B/
    └── ...
```

- **最新バージョン優先**: `バージョン N` フォルダの数字が最大のものを採点対象にする。
- **PDF / 画像対応**: `.pdf` を優先、なければ `.jpg .jpeg .png .webp .bmp .tiff` を採点。
- 課題フォルダが無い／中身が空の学生は「未提出」として記録。

> 提出物の構造が違う場合は、走査ロジック（`grader.py` のフォルダ探索部分）を自分の構造に合わせて調整してください。AIツールに「このフォルダ構造に合わせて」と伝えると早いです。

### 学生名簿 `student_list.csv`
- 1列目: ローマ字氏名、2列目: 漢字氏名。
- 集計CSVの `英語名` 列を、フォルダ名（漢字氏名）から引くために使う。名簿が無くても採点自体は動くが `英語名` は空になる。

## クイックスタート（最小手順）

```bash
# 1. 依存インストール
pip install openai pillow pdf2image python-dotenv
#   PDFを扱うため poppler も必要:
#   Mac: brew install poppler   /   Windows: poppler を別途インストール

# 2. .env を作成（.env.example をコピーして編集）
cp .env.example .env

# 3. VLM の APIサーバーを起動（LM Studio / vLLM など。画像入力対応モデルを選ぶ）

# 4. grader.py 冒頭の TARGET_ASSIGNMENT を採点したい課題名にする
#    grading_criteria/<課題名>.md を用意し、assignment_config.json に用紙向きを登録

# 5. 実行（まずは GRADE_LIMIT = 3 などで小さく試す）
python grader.py
```

## `.env` の設定

`.env.example` をコピーして環境に合わせて編集する。

| 変数 | 説明 |
|------|------|
| `API_BASE_URL` | VLMのAPIエンドポイント（例 `http://localhost:1234/v1`）。**必須** |
| `API_KEY` | APIキー（LM Studio等ではダミーで可）。**必須** |
| `MODEL_NAME` | 使用するモデル名。**必須**（画像を読めるVLMを指定すること） |
| `SUBMITTED_DIR` | 提出物ルート。省略時 `./Submitted files` |
| `STUDENT_LIST_PATH` | 名簿CSVのパス。省略時 `./student_list.csv` |
| `SUMMARY_DIR` | 集計CSVの出力先。省略時 `./grading_summary` |

## 採点する課題の指定

1. `grading_criteria/<課題名>.md` を作る（課題名はフォルダ名と一致させる）。既存の `grading_criteria/` のファイルを雛形にする。
   - 課題特有の観点は**この基準mdに書く**。`grader.py` 本体にはハードコーディングしない方針。
2. `assignment_config.json` に課題名と用紙向き（`landscape`＝横長 / `portrait`＝縦長）を登録する。ここに無い課題名を指定するとエラーになる。実画像の向きと設定が違う場合、90度回転してから採点する。
   ```json
   {
       "事前課題1": "landscape",
       "リフレクションシート1": "portrait"
   }
   ```
3. `grader.py` 冒頭の `TARGET_ASSIGNMENT` をその課題名にする。

## `grader.py` の主な設定定数（冒頭）

| 定数 | 役割 |
|------|------|
| `TARGET_ASSIGNMENT` | 採点する課題名（`GRADE_ALL_ASSIGNMENTS=False` のとき有効） |
| `GRADE_ALL_ASSIGNMENTS` | `True` で `assignment_config.json` の全課題を順に採点（`TARGET_ASSIGNMENT` は無視） |
| `SKIP_ALREADY_GRADED` | `True` で成功済みはスキップ。エラーの学生は再採点される |
| `SUMMARY_REGRADED_ONLY` | `True` で集計CSVに「今回採点した行のみ」を出力（未提出・スキップ行を除外） |
| `GRADE_LIMIT` | 1回で採点する最大人数。`None` で全員。デバッグ時は `3` 等に |
| `MAX_TOKENS` / `TEMPERATURE` / `REQUEST_TIMEOUT` | VLM呼び出しパラメータ |

> **本番採点前に必ず `SKIP_ALREADY_GRADED` と `GRADE_LIMIT` の設定を確認すること。**

## 出力

### 各学生の課題フォルダ内
- `grading_result.json` — 機械可読・再処理用
- `grading_result.md` — 人間可読・確認用

### 全体集計
- `grading_summary/summary_<課題名>_<日時>.csv`（課題ごとに1ファイル）
- 列: `課題名 / 氏名 / 英語名 / 状態 / 提出ファイル / 点数 / 評価理由 / フィードバック`
- 状態は「採点完了 / 採点済みスキップ / 未提出 / エラー」。
- `英語名` は `student_list.csv` の1列目（ローマ字氏名）を漢字氏名で引いて出力。

## 課題が変わったら
1. `grading_criteria/<新しい課題名>.md` を作成
2. `assignment_config.json` に課題名と用紙向きを追加
3. `grader.py` の `TARGET_ASSIGNMENT` を新しい課題名に変更
4. 実行

## ディレクトリ構成

```
work_grading/
├── grader.py                   # メインスクリプト（コア）
├── test_single.py              # 動作確認用（1ファイル試す）
├── assignment_config.json      # 課題ごとの用紙向き設定
├── student_list.csv            # 学生名簿（git管理外・個人情報）
├── .env                        # API設定（git管理外・秘密情報）
├── .env.example                # .envのテンプレート
├── grading_criteria/           # 採点基準（課題ごとに1ファイル）
├── Submitted files/            # 提出物（git管理外・個人情報）
├── grading_summary/            # 全体集計CSV出力先（自動生成・git管理外・個人情報）
├── add_teams_columns.py        # ↓この授業固有（任意）
└── teams_autofill.py           # ↓この授業固有（任意）
```

---

# 応用: 全課題の一括採点・エラーのみ再採点

`grader.py` 冒頭の3定数を組み合わせると、「成功はスキップ・エラーのみ全課題まとめて再採点」ができる。

```python
GRADE_ALL_ASSIGNMENTS = True   # 全課題を順に処理
SKIP_ALREADY_GRADED   = True   # 成功済みはスキップ、エラーのみ再採点
SUMMARY_REGRADED_ONLY = True   # 集計CSVは今回採点した行のみ
```

再採点の集計を既存の結果と混ぜたくない場合は、`.env` の `SUMMARY_DIR` を空の専用フォルダに向ける（例 `SUMMARY_DIR=./regrade_summaries`）。エラーがあった課題だけ `summary_<課題名>_<日時>.csv` が1課題1ファイルで出力される。終わったら設定を元に戻す。

---

# 応用（この授業固有）: Teams への点数入力

> **これ以降は Microsoft Teams（Web）の成績ページへ点数を自動入力するための補助**です。
> Teamsの画面構造・課題の並びに依存するため、別環境ではそのまま動きません。仕組みの参考に。

## `add_teams_columns.py` — 旧フォーマットCSVへ列を追加

過去に採点した旧フォーマット集計CSV（`氏名,状態,...`）に `課題名`・`英語名` 列を足して `result_summaries_teams/` に出力する。**元ファイルは変更しない**。

```bash
python add_teams_columns.py
```

- `課題名`: ファイル名 `summary_<課題名>_<日時>.csv` から抽出
- `英語名`: `student_list.csv` の1列目を漢字氏名で引く
- 入力元・出力先・名簿パスはスクリプト冒頭の `INPUT_DIR` / `OUTPUT_DIR` / `STUDENT_LIST_PATH` で変更

> 現在の `grader.py` は最初から `課題名`・`英語名` 列を出すので、新しい採点結果にこのスクリプトは不要。主に**過去の旧フォーマットCSVの変換**用。

## `teams_autofill.py` — Teams（Web）へ点数を自動入力

Teams採点用CSVを読み、成績ページの各入力欄に点数を自動入力する。各入力欄の `aria-label`（例 `YAMADA, Taro の 9 点中の成績`）から学生名を読み取り、CSVの `英語名` と照合してから入力するので、順序ズレによる誤入力を防ぐ。

### 事前準備（Windows上で操作）
1. Chromeをリモートデバッグ有効で起動（既存のChromeは先に閉じる）:
   ```
   "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\chrome-teams-profile"
   ```
2. そのChromeでTeamsにログインし、対象課題の「成績」ページを開く。
3. Playwrightを導入（CDP接続のみなので `playwright install` は不要）:
   ```
   pip install playwright
   ```

### 実行（半自動バッチモード / 多数の課題向け・推奨）
`BATCH_MODE = True`（既定）で、1回の起動で全課題を続けて入力できる。
1. Teamsで採点したい課題の「成績」ページを開く。
2. `python teams_autofill.py` を実行。
3. プロンプトで **Enter** を押すと、開いている課題のタイトルを検知し、`TEAMS_CSV_DIR`（既定 `result_summaries_teams/`）から該当CSVを**自動選択**して入力。
4. Teamsで**次の課題ページに切り替えて Enter**、を繰り返す。`q + Enter` で終了。
   - まず `DRY_RUN = True` で突き合わせ結果を確認 → 問題なければ `False` で本番。
   - 該当CSVが無い／複数該当する課題は安全のためスキップし警告する。

### 実行（単一課題モード）
`BATCH_MODE = False` で1課題だけ入力する。`CSV_PATH` を対象CSVに合わせ、`DRY_RUN = True` で確認 → `False` で本番。

### 動作
- **課題確認**: ページの `data-test="assignment-title"` とCSVの `課題名` を照合（`事前課題1` と `事前課題1 最終版` のような部分一致も許容）。不一致なら中断（`FORCE_ASSIGNMENT=True` で続行可）。
- **学生照合**: aria-labelの名前をカンマ除去・全角半角統一・大文字小文字無視で正規化し `英語名` と突き合わせ。完全一致が外れたら、姓名の順序ゆれ（例: CSV `YAMADA Taro` が Teams側で `TARO, Yamada`）に対応するため語の集合（順序無視）で再照合し、**一意に定まるときだけ**採用。該当なし・複数候補・英語名重複はスキップして警告。
- **手動対応表 `NAME_OVERRIDES`**: 綴りミス等で自動照合できない学生（例: Teams `SUZKI, Hanako` ← U抜け → CSV `SUZUKI Hanako`）を明示的に結びつける。`teams_autofill.py` 冒頭に `"Teams側の氏名": "CSVの英語名"` を1行足すだけ。既定は空。同じ学生は全課題で同じ綴りになるため一度書けば全課題で有効（誤入力防止のため綴りミスの自動類推はしない）。
- **入力**: 一致行の `点数` を入力（`0` は入力、空欄はスキップ）。`SKIP_IF_FILLED=True` なら既に値がある欄はスキップ。入力後 `↓` で次の欄へ。
- 最後に「入力/スキップ（理由別）/CSVにあるが画面に現れなかった学生」を集計表示。

> セレクタ・接続先・待機時間は `teams_autofill.py` 冒頭の定数で調整できる。

---

# 引き継ぎ時のチェックリスト（個人情報）

このツールは学生の実名・提出物を扱う。**渡す前に必ず確認すること。**

- [ ] **配布は `git clone` から行う**（`.gitignore` で個人情報ファイルが除外される）。フォルダごとコピー／zipで渡すと、`Submitted files/`・`grading_summary/`・`student_list.csv`・過去の集計などが**そのまま同梱される**ので注意。
- [ ] 渡すコード・READMEに**実名が残っていないか**確認する（サンプル名はプレースホルダ `YAMADA, Taro` 等にしてある）。特に `teams_autofill.py` の `NAME_OVERRIDES` は空にしてから渡す。
- [ ] `.env`（APIキー等の秘密情報）は渡さない。テンプレートの `.env.example` のみ共有する。
- [ ] 自分の授業で使う際は、`student_list.csv` / `Submitted files/` / `grading_summary/` を自分のデータに置き換える（いずれもgit管理外）。
