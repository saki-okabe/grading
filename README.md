# 採点エージェント

ローカルVLMで手書き課題を一括採点するツール。

## ディレクトリ構成

```
work_grading/
├── grader.py                   # メインスクリプト
├── test_single.py              # 動作確認用（1ファイル試す）
├── assignment_config.json      # 課題ごとの用紙向き設定
├── student_list.csv            # 学生名簿（1列目: ローマ字, 2列目: 漢字氏名）
├── .env                        # API設定（git管理外）
├── .env.example                # .envのテンプレート
├── grading_criteria/           # 採点基準（課題ごとに1ファイル）
│   ├── 事前課題1.md
│   ├── 事前課題1　最終版.md
│   └── （課題が増えたらここに追加）
├── Submitted files/            # 提出物（学生フォルダ群、git管理外）
│   ├── 学生A/
│   │   └── 事前課題1/
│   │       └── バージョン 1/
│   │           └── 提出ファイル.pdf
│   └── 学生B/
│       └── ...
└── grading_summary/            # 全体集計CSV出力先（自動生成、git管理外）
    └── summary_事前課題1_YYYYMMDD_HHMMSS.csv
```

## 使い方

### 1. 環境準備
```bash
pip install openai pillow pdf2image python-dotenv
# Mac: brew install poppler
# Windows: poppler を別途インストール
```

### 2. `.env` を作成
`.env.example` をコピーして `.env` を作成し、環境に合わせて編集する：
```
API_BASE_URL=http://localhost:1234/v1
API_KEY=lm-studio
MODEL_NAME=<使用するモデル名>
SUBMITTED_DIR=./Submitted files   # 省略時はデフォルト値を使用
STUDENT_LIST_PATH=./student_list.csv  # 省略時はデフォルト値を使用
```

### 3. APIサーバー起動
LM Studio、vLLM 等で APIサーバーを立ち上げる。

### 4. 採点する課題を指定
`grader.py` の冒頭を編集：
```python
TARGET_ASSIGNMENT = "事前課題1"   # ← 採点したい課題名
```

### 5. 採点基準を用意
`grading_criteria/事前課題1.md` のように、課題名と同じ名前のmdファイルを作る。
（既存のサンプルを参考に編集してください）

### 6. 実行
```bash
python grader.py
```

## 出力

### 各学生の課題フォルダ内
- `grading_result.json` — 機械可読・再処理用
- `grading_result.md`   — 人間可読・確認用

### 全体集計
- `grading_summary/summary_<課題名>_<日時>.csv`（課題ごとに1ファイル）
- 先頭列に `課題名 / 氏名 / 英語名`、続けて全学生の状態（採点完了 / 採点済みスキップ / 未提出 / エラー）と点数を一覧化
- `英語名` は `student_list.csv` の1列目（ローマ字氏名）を漢字氏名で引いて出力

## 仕様

- **最新バージョン優先**: `バージョン N` フォルダの数字が大きい方を採点対象にする
- **採点済みスキップ**: `SKIP_ALREADY_GRADED=True` のとき、成功済み（エラーの無い `grading_result.json` がある）学生は再採点しない。エラー（採点エラー・例外エラー）の学生は再採点対象になる
- **未提出検出**: 課題フォルダ自体が無い、または中身が空の学生は「未提出」として記録
- **PDF / 画像対応**: `.pdf` を優先、なければ `.jpg .jpeg .png .webp .bmp .tiff` を採点
- **向き補正**: `assignment_config.json` に課題ごとの用紙向き（`landscape` / `portrait`）を設定し、実際の画像サイズと異なる場合に90度回転してから採点

## assignment_config.json について

課題ごとに期待する用紙向きを設定するファイル。`landscape`（横長）か `portrait`（縦長）を指定する。
`TARGET_ASSIGNMENT` に指定した課題名がここに登録されていないとエラーになる。

```json
{
    "事前課題1": "landscape",
    "リフレクションシート1": "portrait"
}
```

## 課題が変わったら
1. `grading_criteria/<新しい課題名>.md` を作成
2. `assignment_config.json` に課題名と用紙向きを追加
3. `grader.py` の `TARGET_ASSIGNMENT` を新しい課題名に変更
4. 実行

## 全課題を一括採点 / エラーのみ再採点

`grader.py` 冒頭の3つの定数で挙動を制御する（すべて独立して組み合わせ可能）。

| 定数 | 役割 |
|------|------|
| `GRADE_ALL_ASSIGNMENTS` | `True` で `assignment_config.json` の全課題を順に採点（`TARGET_ASSIGNMENT` は無視） |
| `SKIP_ALREADY_GRADED`   | `True` で成功済みはスキップ。エラーの学生は再採点される |
| `SUMMARY_REGRADED_ONLY` | `True` で集計CSVに「今回採点した行のみ」を出力（未提出・スキップ行は除外） |

### 前回エラーになった課題を再採点し、Teamsに入力するまで（手順まとめ）

1周目でエラー（採点エラー・例外エラー）になった学生だけを全課題まとめて再採点し、
その点数をTeamsに入力するまでの一連の手順。

**方針**: 再採点の出力は **課題ごとに1CSV**（`teams_autofill.py` の半自動バッチモードと
同じ形式）。`grader.py` は `課題名`・`英語名` 列を出すので `add_teams_columns.py` は不要。
ただし**既存の結果CSVとは別の空フォルダに出力する**こと（同じ課題名のCSVが2つ並ぶと、
バッチ入力時に「複数該当」で安全スキップされるため）。

#### ステップ1: 再採点専用の空フォルダに出力先を向ける
`.env` の `SUMMARY_DIR` を、空の専用フォルダにする（例）:
```
SUMMARY_DIR=./regrade_summaries
```

#### ステップ2: エラー分だけ再採点する
`grader.py` 冒頭の3つの定数をすべて `True` にして実行:
```python
GRADE_ALL_ASSIGNMENTS = True   # 全課題を順に処理
SKIP_ALREADY_GRADED   = True   # 成功済みはスキップ、エラーのみ再採点
SUMMARY_REGRADED_ONLY = True   # 集計CSVは今回採点した行のみ
```
```
python grader.py
```
- 成功済みの学生は即スキップ（VLMを呼ばない）、前回エラーの学生だけ再採点される
- エラーがあった課題だけ、`regrade_summaries/summary_<課題名>_<日時>.csv` が
  1課題1ファイルで出力される（`課題名`・`英語名` 列を含む。エラーが無かった課題はCSVを作らない）
- デバッグ時は `GRADE_LIMIT = 3` などで小さく試し、本番前に `None` に戻す

#### ステップ3: 再採点した点数をTeamsに入力する
`teams_autofill.py` 冒頭を次のようにする:
```python
BATCH_MODE = True
TEAMS_CSV_DIR = Path("./regrade_summaries")   # ステップ1の出力フォルダ
DRY_RUN = True                                 # まず確認。問題なければ False
```
あとは通常のバッチ入力と同じ（「Teams（Web）へ点数を自動入力する」の節を参照）。
Teamsで課題ページを開いて Enter を繰り返すだけ。今回は前回エラーだった数名だけが入力され、
既に入力済みの学生は `SKIP_IF_FILLED=True` により自動でスキップされる。

#### 終わったら戻す
- `.env` の `SUMMARY_DIR` を元の設定に戻す
- `grader.py` の3定数・`GRADE_LIMIT`、`teams_autofill.py` の `TEAMS_CSV_DIR`・`DRY_RUN` を元に戻す

## Teams入力用に既存の集計CSVへ列を追加する

1周目の集計CSV（`result_summaries/` 配下、旧フォーマット `氏名,状態,...`）に
`課題名` と `英語名` を追加した新CSVを作るスクリプト。**元ファイルは変更せず**
`result_summaries_teams/` に同名で出力する。

```bash
python add_teams_columns.py
```

- `課題名`: ファイル名 `summary_<課題名>_<日時>.csv` から抽出（末尾のタイムスタンプのみ除去）
- `英語名`: `student_list.csv` の1列目（ローマ字氏名）を漢字氏名で引く
- 出力カラム順: `課題名, 氏名, 英語名, 状態, 提出ファイル, 点数, 評価理由, フィードバック`
- `student_list.csv` に見つからない氏名は英語名を空にし、件数を警告表示する

入力元・出力先・名簿パスは `add_teams_columns.py` 冒頭の `INPUT_DIR` /
`OUTPUT_DIR` / `STUDENT_LIST_PATH` で変更できる。

> `grader.py` 自体も `英語名` 列を出力するようになったため、以降の採点CSVには
> 最初から `課題名`・`英語名` が入る。このスクリプトは主に**過去に採点済みの
> 旧フォーマットCSV**を変換するために使う。

## Teams（Web）へ点数を自動入力する

`add_teams_columns.py` が出力したTeams採点用CSVを使い、Teamsの成績ページの
入力欄に点数を自動入力するスクリプト（`teams_autofill.py`）。
各入力欄の `aria-label`（例 `AIBA, Rino の 9 点中の成績`）から学生名を読み取り、
CSVの `英語名` と照合してから点数を入れるので、順序ズレによる誤入力を防ぐ。

### 事前準備（すべてWindows上で操作）
1. Chromeをリモートデバッグ有効で起動（既存のChromeは先に閉じる）:
   ```
   "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\chrome-teams-profile"
   ```
2. そのChromeでTeamsにログインし、対象課題の「成績」ページを開く。
3. Playwrightを導入（CDP接続のみなので `playwright install` は不要）:
   ```
   pip install playwright
   ```

### 実行（半自動バッチモード / 21課題向け・推奨）
`BATCH_MODE = True`（既定）にすると、1回の起動で全課題を続けて入力できる。
CSVを課題ごとに指定し直す必要はない。
1. Teamsで採点したい課題の「成績」ページを開く。
2. 実行する:
   ```
   python teams_autofill.py
   ```
3. プロンプトで **Enter** を押すと、開いている課題のタイトルを検知し、
   `TEAMS_CSV_DIR`（既定 `result_summaries_teams/`）から該当CSVを**自動選択**して入力。
4. Teamsで**次の課題ページに切り替えて Enter** を押す、を繰り返す。`q + Enter` で終了。
   - まず `DRY_RUN = True` にして各課題の突き合わせ結果を確認してから、`False` で本番。
   - 該当CSVが見つからない／複数該当する課題は安全のためスキップし警告する。

### 実行（単一課題モード）
`BATCH_MODE = False` にすると従来どおり1課題だけを入力する。
1. `CSV_PATH` を対象課題のCSVに合わせる。
2. まず `DRY_RUN = True` で「誰に何点入るか」を確認 → 問題なければ `False` で本番。
   ```
   python teams_autofill.py
   ```

### 動作
- **課題確認**: ページの `data-test="assignment-title"` のテキストとCSVの `課題名` を照合（`事前課題1` と `事前課題1 最終版` のような部分一致も許容）。不一致なら中断（`FORCE_ASSIGNMENT=True` で続行可）
- **学生照合**: aria-labelの名前をカンマ除去・全角半角統一・大文字小文字無視で正規化し、`英語名` と突き合わせ。完全一致が外れた場合は、姓名の順序ゆれ（登録ミスで `ARAI Daisuke` が Teams側で `DAISUKE, Arai` になっている等）に対応するため語の集合（順序無視）で再照合し、**一意に定まるときだけ**採用（ログに「名前順ゆれをtoken一致で対応」と表示）。該当なし・複数候補・英語名重複はスキップして警告
- **手動対応表 `NAME_OVERRIDES`**: 綴りミス等で自動照合できない学生（例: Teams `ISHIZKA, Rinka` ← U抜け → CSV `ISHIZUKA Rinka`）を明示的に結びつける。`teams_autofill.py` 冒頭に `"Teams側の氏名": "CSVの英語名"` を1行足すだけ。同じ学生は全課題で同じ綴りになるため一度書けば全課題で有効（綴りミスの自動類推は誤入力防止のため行わない）
- **入力**: 一致した行の `点数` を入力（`0` は入力、空欄はスキップ）。`SKIP_IF_FILLED=True` なら既に値がある欄はスキップ。入力後 `↓` キーで次の欄へ移動し、進まなくなったら終了
- 最後に「入力/スキップ（理由別）/CSVにあるが画面に現れなかった学生」を集計表示

> セレクタや接続先・待機時間は `teams_autofill.py` 冒頭の定数で調整できる。
