# 共同開発の進め方

Python 3.10 以上を使用し、リポジトリのルートから実行する。

```bash
python -m venv .venv
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
# macOS / Linux: source .venv/bin/activate
python -m pip install -r requirements-teams.txt
python -m unittest discover -s tests -v
```

テストは架空の識別子・一時ディレクトリ・VLMのモックのみを使用する。
APIサーバー、APIキー、名簿、Poppler、Teamsログインは不要。
実際のPDF変換には別途 Poppler が必要。

## ブランチとレビュー

1. main を更新し、`git switch -c feature/<作業名>` で作業ブランチを作る。
2. 課題設定・採点基準、採点処理、Teams連携の変更は可能な範囲でPRを分ける。
3. 個人ごとの課題選択や入力・出力先は `.env` に設定する。
4. テスト後、変更したファイルだけを `git add <ファイル>` で追加する。
5. `git diff --cached` で内容を確認し、作業ブランチをPushしてmain向けPRを作る。
6. PRに目的、採点への影響、テスト結果、手動確認の有無を記載してレビューを受ける。

main への直接Pushは行わない。リポジトリ管理者にはmainの保護ルールで
PRレビューと `regression` のCI成功を必須にすることを推奨する（このPRでは設定しない）。

## データの扱い

`.env*`（`.env.example` を除く）、名簿、提出物、採点結果をコミットしない。
授業ごとのデータや氏名の手動対応表もローカルで管理する。
新しいテストは実在の学生情報を使わず、一時ディレクトリ内で完結させる。
任意の入出力先は `local_data/` 配下にするとgit管理から除外できる。
別の場所を指定する場合は、対応する `.gitignore` も確認する。
`.gitignore` は既に追跡されているファイルや明示的な強制追加を防げないので、
コミット直前の差分確認は省略しない。

## 構成と改善方針

| ファイル | 責務 |
| --- | --- |
| `grader.py` | 名簿順の提出物探索、画像変換、VLM呼出し、結果・集計保存 |
| `grading_criteria/` | 課題固有の採点基準と応答JSON形式 |
| `assignment_config.json` | 課題ごとの用紙向き |
| `add_teams_columns.py` | 過去の集計CSVの変換 |
| `teams_autofill.py` | Teamsの学生照合・点数入力 |
| `teams_download.py` | 開いているTeams画面からPDFを取得 |
| `tests/` | 実データ・外部通信を使わない回帰テスト |

最初の改善として、実行設定の環境変数化、import時の副作用削減、依存定義、
Windows/Linux CI、回帰テストとPRテンプレートを追加している。
スクリプトの起動方法、採点プロンプト、出力形式、既定の実行モードは維持する。
`grader` をライブラリとして利用するときは、実行前に `configure_runtime()` を呼ぶ。

今後は回帰テストを足しながら、探索・画像処理・VLMクライアント・出力を
モジュールへ分割すると、担当者間の編集競合を減らせる。
続いてTeamsの設定と氏名対応表の外部化、依存のロックと更新手順、
Poppler付きPDFテスト、匿名化した評価セットによるモデル品質評価を別PRで進める。
Teamsスクリプトは画面依存のため、単体テストの成功だけで実運用可とは判断しない。
`teams_download.py` はimport時にブラウザへ接続するので、開発中も直接importしない。
