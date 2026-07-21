"""
手書き課題の採点スクリプト（実用版）

「Submitted files/」配下の学生フォルダを走査し、指定した課題の最新バージョンを
読み取って採点する。結果は各学生フォルダ内と、全体集計CSVの両方に保存される。

ディレクトリ構造の前提:
    Submitted files/
        学生氏名/
            課題名/                ← TARGET_ASSIGNMENT で指定
                バージョン 1/
                    提出ファイル.pdf
                バージョン 2/      ← 数字の大きい方を採点
                    提出ファイル.pdf

採点基準は grading_criteria/<課題名>.md から読み込む。
"""
import os
from openai import OpenAI
from pathlib import Path
from pdf2image import convert_from_path
from PIL import Image
import base64
import io
import json
import csv
import re
from datetime import datetime
from dotenv import load_dotenv

# ============================================================
# 設定
# ============================================================
load_dotenv(override=True)  # .envファイルから環境変数を読み込む

def _require_env(key: str) -> str:
    """必須の環境変数を取得。未設定なら明確なエラーで停止する。"""
    value = os.getenv(key)
    if not value:
        raise RuntimeError(
            f"環境変数 {key} が設定されていません。\n"
            f".envファイルを確認するか、.env.exampleを参考に作成してください。"
        )
    return value

API_BASE_URL = _require_env("API_BASE_URL")
API_KEY      = _require_env("API_KEY")
MODEL_NAME   = _require_env("MODEL_NAME")

# 採点する課題名(学生フォルダ直下のサブフォルダ名と一致させる)
# GRADE_ALL_ASSIGNMENTS = False のときに採点する対象。
TARGET_ASSIGNMENT = "リフレクションシート7"

# 全課題を一括採点するかどうか
# True : assignment_config.json に登録された全課題を順に採点する（TARGET_ASSIGNMENTは無視）
# False: TARGET_ASSIGNMENT の1課題のみ採点する
# 用途: 前回エラーになった課題を全課題まとめて再採点したいとき、
#       GRADE_ALL_ASSIGNMENTS=True / SKIP_ALREADY_GRADED=True / SUMMARY_REGRADED_ONLY=True の
#       3つを有効にすると「成功はスキップ・エラーのみ再採点」が全課題で走る。
GRADE_ALL_ASSIGNMENTS = False

# VLM呼び出しパラメータ
MAX_TOKENS = 8000              # 推論モデルはthinkingに大量消費するため余裕を持たせる
TEMPERATURE = 0.2              # 採点は安定性重視で低めに
MAX_RETRY = 1                  # 打ち切られた場合のリトライ回数（合計試行は MAX_RETRY+1 回）
REQUEST_TIMEOUT = 3600         # 1件あたりのタイムアウト秒数（60分）

# 採点済みファイルがある学生をスキップするかどうか
# True : 「成功済み」の学生は採点しない（途中再開・大量処理・エラー再採点向け）
#        ※ is_already_graded がエラー付きJSONを未採点扱いにするため、
#          エラー（採点エラー・例外エラー）の学生は True でも再採点される
# False: 既に採点済みでも再採点する（モデル比較・採点基準の再評価向け、結果は上書き）
SKIP_ALREADY_GRADED = False

# 集計CSVに「今回実際に採点した学生の行」だけを出力するかどうか
# True : 未提出・採点済みスキップの行はCSVに含めず、今回VLMを実行した行のみ出力
#        （エラー再採点時に「どのエラーが解消したか」を確認しやすい）
# False: 従来どおり全学生の行を出力する（完全な集計）
SUMMARY_REGRADED_ONLY = False


# 1回の実行で採点する最大人数（デバッグ・動作確認用）
# None  : 全員を採点する（本番運用）
# 整数  : 指定人数まで採点したら処理を打ち切る（例: 3 で3人分のみ採点）
# 注意: 「未提出」「採点済みスキップ」はカウントされず、実際に採点した人数のみカウントする
GRADE_LIMIT = None

# ディレクトリ
#SUBMITTED_DIR = Path("./Submitted files")           # 提出物のルート
SUBMITTED_DIR  = Path(os.getenv("SUBMITTED_DIR", "./Submitted files"))
CRITERIA_DIR = Path("./grading_criteria")           # 採点基準のルート
SUMMARY_DIR = Path(os.getenv("SUMMARY_DIR", "./grading_summary"))  # 全体集計の保存先
SUMMARY_DIR.mkdir(exist_ok=True)

# 学生名簿CSV（1列目: ローマ字氏名, 2列目: 漢字氏名）
STUDENT_LIST_PATH = Path(os.getenv("STUDENT_LIST_PATH", "./student_list.csv"))

# 採点結果ファイル名（各学生の課題フォルダ内に保存される）
RESULT_JSON_NAME = "grading_result.json"            # 機械可読（再処理用）
RESULT_MD_NAME = "grading_result.md"                # 人間可読（確認用）

# 対応する画像形式
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}
PDF_EXTS = {".pdf"}

# 課題ごとの用紙向き設定ファイル
ASSIGNMENT_CONFIG_PATH = Path("./assignment_config.json")

def _load_assignment_config() -> dict[str, str]:
    if not ASSIGNMENT_CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"{ASSIGNMENT_CONFIG_PATH} が見つかりません。"
            "用紙向き設定ファイルを作成してください。"
        )
    with ASSIGNMENT_CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)

ASSIGNMENT_CONFIG = _load_assignment_config()


# ============================================================
# 採点基準の読み込み
# ============================================================
def load_criteria(assignment_name: str) -> str:
    """grading_criteria/<課題名>.md から採点基準を読み込む。"""
    criteria_path = CRITERIA_DIR / f"{assignment_name}.md"
    if not criteria_path.exists():
        raise FileNotFoundError(
            f"採点基準ファイルが見つかりません: {criteria_path}\n"
            f"{CRITERIA_DIR}/ に '{assignment_name}.md' を作成してください。"
        )
    return criteria_path.read_text(encoding="utf-8")


def build_prompt(criteria_text: str) -> str:
    """
    採点基準を埋め込んだプロンプトを生成。

    課題特有の観点名や項目はすべて採点基準md側で定義する。
    grader.py側にはハードコーディングしないことで、課題が変わっても
    grader.py本体を変更せず、採点基準mdを差し替えるだけで対応できる。
    """
    return f"""あなたは大学の採点者です。以下の採点基準に従って、画像の手書き課題を採点してください。

===== 採点基準 =====
{criteria_text}
===== 採点基準ここまで =====

【重要な指示】
- 採点基準内の「出力JSONテンプレート」に厳密に従ってJSON形式で回答すること
- テンプレートのキー名は一字一句変えずに使うこと（採点基準で定義されたもの以外のキーを勝手に追加・変更しないこと）
- テンプレート内の `<...>` の部分のみ、実際の判定結果に置き換えること
- `<...>` のような山括弧プレースホルダーや、`○`などの記号を出力結果に残さないこと
- 値の選択肢が指定されている項目（例: 「あり または なし」）は、その選択肢のいずれかを選ぶこと
- 整数を入れる項目には実際の整数を入れること
- 前置き・解説・思考過程は出力に含めず、JSONのみを返すこと
- 日本語で記述すること
"""


# ============================================================
# 学生名簿
# ============================================================
def load_student_list(csv_path: Path) -> list[str]:
    """student_list.csvから漢字氏名の一覧を読み込む（2列目）。"""
    if not csv_path.exists():
        raise FileNotFoundError(f"学生名簿が見つかりません: {csv_path}")
    names = []
    with csv_path.open(encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 2 and row[1].strip():
                names.append(row[1].strip())
    return names


def load_english_name_map(csv_path: Path) -> dict[str, str]:
    """student_list.csvから「漢字氏名 → ローマ字氏名」の対応を読み込む。

    student_list.csv は 1列目=ローマ字氏名, 2列目=漢字氏名 の想定。
    集計CSVの氏名（漢字）から英語表記を引くために使う。
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"学生名簿が見つかりません: {csv_path}")
    mapping: dict[str, str] = {}
    with csv_path.open(encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 2 and row[1].strip():
                mapping[row[1].strip()] = row[0].strip()
    return mapping


# ============================================================
# ディレクトリ走査
# ============================================================
def parse_version_number(version_dir: Path) -> int:
    """「バージョン 1」「バージョン 2」等から数字を抽出。失敗したら-1。"""
    match = re.search(r"\d+", version_dir.name)
    return int(match.group()) if match else -1


def find_latest_submission(student_assignment_dir: Path) -> Path | None:
    """
    課題フォルダ内の最新バージョンの提出ファイル（PDFまたは画像）を返す。
    存在しない場合は None。
    """
    if not student_assignment_dir.exists():
        return None

    # バージョンフォルダを取得
    version_dirs = [d for d in student_assignment_dir.iterdir() if d.is_dir()]
    if not version_dirs:
        return None

    # 数字の大きい方が新しい
    latest_dir = max(version_dirs, key=parse_version_number)

    # PDFを優先、なければ画像
    pdfs = sorted(
        [f for f in latest_dir.iterdir() if f.is_file() and f.suffix.lower() in PDF_EXTS]
    )
    if pdfs:
        return pdfs[0]

    images = sorted(
        [f for f in latest_dir.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTS]
    )
    if images:
        return images[0]

    return None


def collect_students(target: str) -> list[dict]:
    """
    student_list.csvの順番に従って学生一覧を取得し、
    指定課題の提出ファイル情報をまとめる。
    名簿に載っているが提出フォルダが存在しない学生は除外する（スキップ）。

    Returns:
        [
            {
                "name": "氏名",                 # 漢字氏名
                "english_name": "英語名",       # ローマ字氏名（名簿に無ければ空文字）
                "assignment_dir": Path,         # 結果保存先
                "submission": Path or None,     # 採点対象ファイル
            },
            ...
        ]
    """
    if not SUBMITTED_DIR.exists():
        raise FileNotFoundError(f"提出物フォルダが存在しません: {SUBMITTED_DIR}")

    student_names = load_student_list(STUDENT_LIST_PATH)
    english_map = load_english_name_map(STUDENT_LIST_PATH)

    # 提出フォルダ名を辞書化（高速照合用）
    existing_dirs = {d.name: d for d in SUBMITTED_DIR.iterdir() if d.is_dir()}

    students = []
    for name in student_names:
        sdir = existing_dirs.get(name)
        if sdir is None:
            # 名簿にいるが提出フォルダが存在しない → 除外
            continue
        assignment_dir = sdir / target
        submission = find_latest_submission(assignment_dir)
        students.append({
            "name": name,
            "english_name": english_map.get(name, ""),
            "assignment_dir": assignment_dir,
            "submission": submission,
        })
    return students


# ============================================================
# 画像処理
# ============================================================
def _needs_rotation(w: int, h: int, expected: str) -> bool:
    """現在の向きが期待と異なればTrueを返す。"""
    is_landscape = w >= h
    return (expected == "landscape" and not is_landscape) or \
           (expected == "portrait"  and     is_landscape)


def load_images(filepath: Path, assignment_name: str) -> list:
    """PDF/PNG/JPG等を画像リストに変換。assignment_config.jsonの向き設定に従って回転補正する。"""
    if assignment_name not in ASSIGNMENT_CONFIG:
        raise KeyError(
            f"課題 '{assignment_name}' が assignment_config.json に登録されていません。"
        )
    expected = ASSIGNMENT_CONFIG[assignment_name]

    ext = filepath.suffix.lower()
    if ext in PDF_EXTS:
        pages = convert_from_path(filepath, dpi=200)
        result = []
        for page in pages:
            w, h = page.size
            if _needs_rotation(w, h, expected):
                page = page.rotate(90, expand=True)
            result.append(page)
        return result
    elif ext in IMAGE_EXTS:
        image = Image.open(filepath).convert("RGB")
        w, h = image.size
        if _needs_rotation(w, h, expected):
            image = image.rotate(90, expand=True)
        return [image]
    else:
        raise ValueError(f"対応していない形式です: {ext}")


def merge_images_vertically(images: list) -> Image.Image:
    """複数画像を縦に連結して1枚にまとめる。"""
    if len(images) == 1:
        return images[0]
    max_width = max(img.width for img in images)
    total_height = sum(img.height for img in images)
    canvas = Image.new("RGB", (max_width, total_height), "white")
    y = 0
    for img in images:
        canvas.paste(img, (0, y))
        y += img.height
    return canvas


def encode_image_to_base64(image: Image.Image, max_size: int = 1568) -> str:
    """PIL画像をbase64エンコード（必要なら縮小）。"""
    if max(image.size) > max_size:
        ratio = max_size / max(image.size)
        new_size = (int(image.size[0] * ratio), int(image.size[1] * ratio))
        image = image.resize(new_size, Image.LANCZOS)
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# ============================================================
# VLM呼び出し
# ============================================================
def extract_text_from_response(msg) -> str:
    """推論モデル対応: contentが空ならreasoning_contentにフォールバック。"""
    content = msg.content or ""
    if content.strip():
        return content
    reasoning = getattr(msg, "reasoning_content", None) or ""
    return reasoning if reasoning.strip() else ""

def _extract_balanced_json(text: str) -> str | None:
    """
    テキストから最初に現れる「中括弧のバランスが取れたJSONオブジェクト」を抽出する。

    LLMが余計な前置き・後置きをつけたり、JSONの後ろにthinkingの続きや
    別のテキストを書いた場合でも、純粋なJSON部分だけを切り出せる。
    文字列内の `{` `}` は無視する(エスケープも考慮)。
    """
    in_string = False
    escape = False
    depth = 0
    start_index = -1

    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start_index = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start_index >= 0:
                    return text[start_index:i + 1]
    return None

def parse_grading_result(raw_text: str) -> dict:
    """LLM応答からJSON部分を抽出してパース。"""
    if not raw_text:
        return {"error": "VLMから空のレスポンスが返りました", "_raw": ""}

    # 1. ```json ... ``` のコードブロックがあれば優先して使う
    json_str = None
    if "```json" in raw_text:
        start = raw_text.find("```json") + len("```json")
        end = raw_text.find("```", start)
        if end > start:
            candidate = raw_text[start:end].strip()
            # コードブロック内でも、念のためバランスの取れたJSONを再抽出
            extracted = _extract_balanced_json(candidate)
            json_str = extracted if extracted else candidate

    # 2. コードブロックなしの場合、テキスト全体からバランスの取れたJSONを抽出
    if json_str is None:
        json_str = _extract_balanced_json(raw_text)

    # 3. それでも見つからない場合、最後の手段として { } の範囲
    if json_str is None:
        start = raw_text.rfind("{")
        end = raw_text.rfind("}") + 1
        json_str = raw_text[start:end] if start >= 0 and end > start else "{}"

    try:
        result = json.loads(json_str)
        return result
    except json.JSONDecodeError as e:
        return {"error": f"JSONパース失敗: {e}", "_raw": raw_text}


def grade_image(client: OpenAI, image: Image.Image, prompt: str) -> dict:
    """1枚の画像をVLMに送って採点結果を取得。打ち切り時はリトライする。"""
    img_b64 = encode_image_to_base64(image)
    messages = [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
            {"type": "text", "text": prompt},
        ],
    }]

    last_warning = None
    for attempt in range(MAX_RETRY + 1):
        if attempt > 0:
            print(f"    リトライ {attempt}/{MAX_RETRY}...")

        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
        )
        msg = response.choices[0].message
        finish_reason = response.choices[0].finish_reason

        raw_text = extract_text_from_response(msg)
        result = parse_grading_result(raw_text)

        # 成功条件: パースできて、点数が抽出できている
        if "error" not in result and result.get("点数") is not None:
            if finish_reason == "length":
                result["_warning"] = "max_tokensに達したが採点結果は取得できました"
            return result

        # 打ち切られたがJSON抽出不可 → リトライ
        last_warning = f"finish_reason={finish_reason}, " + result.get("error", "点数抽出失敗")
        if finish_reason != "length":
            # 打ち切り以外のエラーはリトライしても変わらない
            break

    # すべて失敗
    if "error" not in result:
        result = {"error": last_warning or "採点失敗", "_raw": result.get("_raw", "")}
    if finish_reason == "length":
        result["_warning"] = "max_tokensに達して打ち切られました（リトライ済み）"
    return result


# ============================================================
# 採点結果の保存
# ============================================================
def save_result_files(assignment_dir: Path, student_name: str,
                      submission_path: Path, result: dict,
                      assignment_name: str):
    """各学生の課題フォルダ内に JSON と Markdown を保存。"""
    assignment_dir.mkdir(parents=True, exist_ok=True)

    # ---- JSON（機械可読） ----
    json_data = {
        "学生": student_name,
        "課題": assignment_name,
        "提出ファイル": submission_path.name,
        "採点日時": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "モデル": MODEL_NAME,
        "採点結果": result,
    }
    (assignment_dir / RESULT_JSON_NAME).write_text(
        json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ---- Markdown（人間可読） ----
    md_lines = [
        f"# 採点結果: {student_name}",
        "",
        f"- **課題**: {assignment_name}",
        f"- **提出ファイル**: {submission_path.name}",
        f"- **採点日時**: {json_data['採点日時']}",
        f"- **モデル**: {MODEL_NAME}",
        "",
        "## 点数",
        "",
        f"**{result.get('点数', '?')} 点**",
        "",
    ]

    aspects = result.get("観点別評価")
    if isinstance(aspects, dict) and aspects:
        md_lines += ["## 観点別評価", ""]
        for k, v in aspects.items():
            md_lines.append(f"- **{k}**: {v}")
        md_lines.append("")

    if result.get("評価理由"):
        md_lines += ["## 評価理由", "", str(result["評価理由"]), ""]

    if result.get("フィードバック"):
        md_lines += ["## フィードバック", "", str(result["フィードバック"]), ""]

    if result.get("error"):
        md_lines += ["## ⚠ エラー", "", str(result["error"]), ""]
        if result.get("_raw"):
            md_lines += ["### 生レスポンス（デバッグ用）", "", "```", str(result["_raw"])[:2000], "```", ""]

    if result.get("_warning"):
        md_lines += ["## ⚠ 警告", "", str(result["_warning"]), ""]

    (assignment_dir / RESULT_MD_NAME).write_text("\n".join(md_lines), encoding="utf-8")


def is_already_graded(assignment_dir: Path) -> bool:
    """採点済みかどうかを判定する。

    grading_result.json が存在し、かつ採点結果にエラーが無い場合のみ
    「採点済み」とみなす。エラー付きJSON（採点エラー）や、壊れて読めない
    JSONは未採点扱いとし、SKIP_ALREADY_GRADED=True でも再採点対象にする。
    （例外エラーはそもそもJSONが残らないため、ここではFalse=未採点になる）
    """
    json_path = assignment_dir / RESULT_JSON_NAME
    if not json_path.exists():
        return False
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return False  # 壊れて読めないJSONは再採点対象
    result = data.get("採点結果", {})
    return isinstance(result, dict) and "error" not in result


# ============================================================
# メイン処理
# ============================================================
SUMMARY_FIELDNAMES = ["課題名", "氏名", "英語名", "状態", "提出ファイル", "点数", "評価理由", "フィードバック"]


def grade_one_assignment(client: OpenAI, assignment_name: str,
                         stats: dict) -> bool:
    """1課題分の採点を実行し、課題ごとの集計CSVを出力する。

    stats は複数課題にまたがる集計カウンタ（呼び出し側で共有・加算される）。
    GRADE_LIMIT に達して処理全体を打ち切るべき場合は True を返す。
    """
    print("=" * 70)
    print(f"■ 課題: {assignment_name}")
    print("=" * 70)

    # 採点基準を読み込み（無い課題はスキップして次へ）
    try:
        criteria = load_criteria(assignment_name)
    except FileNotFoundError as e:
        print(f"⚠ 採点基準が見つからないためこの課題をスキップします: {e}\n")
        return False
    prompt = build_prompt(criteria)
    print(f"採点基準を読み込みました ({len(criteria)} 文字)")

    # 学生一覧を取得
    students = collect_students(assignment_name)
    print(f"学生数: {len(students)}\n")

    summary_rows = []
    limit_reached = False

    for i, s in enumerate(students, 1):
        name = s["name"]
        english_name = s["english_name"]
        assignment_dir = s["assignment_dir"]
        submission = s["submission"]
        print(f"[{i}/{len(students)}] {name}")

        # ---- 未提出 ----
        if submission is None:
            print(f"  → 提出なし\n")
            stats["no_submission"] += 1
            if not SUMMARY_REGRADED_ONLY:
                summary_rows.append({
                    "課題名": assignment_name,
                    "氏名": name,
                    "英語名": english_name,
                    "状態": "未提出",
                    "提出ファイル": "",
                    "点数": 0,
                    "評価理由": "",
                    "フィードバック": "",
                })
            continue

        # ---- 採点済みスキップ ----
        if SKIP_ALREADY_GRADED and is_already_graded(assignment_dir):
            print(f"  → 採点済み（スキップ）\n")
            stats["skipped"] += 1
            if not SUMMARY_REGRADED_ONLY:
                # 既存結果を集計に含める
                try:
                    existing = json.loads((assignment_dir / RESULT_JSON_NAME).read_text(encoding="utf-8"))
                    r = existing.get("採点結果", {})
                    summary_rows.append({
                        "課題名": assignment_name,
                        "氏名": name,
                        "英語名": english_name,
                        "状態": "採点済み（前回分）",
                        "提出ファイル": existing.get("提出ファイル", submission.name),
                        "点数": r.get("点数", ""),
                        "評価理由": r.get("評価理由", ""),
                        "フィードバック": r.get("フィードバック", ""),
                    })
                except Exception:
                    summary_rows.append({
                        "課題名": assignment_name,
                        "氏名": name, "英語名": english_name, "状態": "採点済み（読込失敗）",
                        "提出ファイル": submission.name, "点数": "", "評価理由": "", "フィードバック": "",
                    })
            continue

        # ---- 採点実行 ----
        try:
            print(f"  ファイル: {submission.name}")
            images = load_images(submission, assignment_name)
            print(f"  ページ数: {len(images)}")
            target_image = merge_images_vertically(images)
            print(f"  VLMで採点中...")
            result = grade_image(client, target_image, prompt)

            save_result_files(assignment_dir, name, submission, result, assignment_name)

            if "error" in result:
                print(f"  → 採点エラー: {result['error']}\n")
                stats["error"] += 1
                summary_rows.append({
                    "課題名": assignment_name,
                    "氏名": name, "英語名": english_name, "状態": "採点エラー",
                    "提出ファイル": submission.name,
                    "点数": "", "評価理由": result["error"], "フィードバック": "",
                })
            else:
                score = result.get("点数", "?")
                print(f"  → 点数: {score}点\n")
                stats["graded"] += 1
                summary_rows.append({
                    "課題名": assignment_name,
                    "氏名": name, "英語名": english_name, "状態": "採点完了",
                    "提出ファイル": submission.name,
                    "点数": score,
                    "評価理由": result.get("評価理由", ""),
                    "フィードバック": result.get("フィードバック", ""),
                })

        except Exception as e:
            print(f"  → 例外: {e}\n")
            stats["error"] += 1
            summary_rows.append({
                "課題名": assignment_name,
                "氏名": name, "英語名": english_name, "状態": "例外エラー",
                "提出ファイル": submission.name if submission else "",
                "点数": "", "評価理由": str(e), "フィードバック": "",
            })

        # ---- 採点上限チェック（デバッグ用） ----
        # ここに来る = 実際にVLM呼び出しを試みた（成功・エラー問わず）
        # 「未提出」「採点済みスキップ」は上の continue で飛ばされているのでカウントされない
        # GRADE_LIMIT は複数課題にまたがる合計試行数で判定する
        attempts = stats["graded"] + stats["error"]
        if GRADE_LIMIT is not None and attempts >= GRADE_LIMIT:
            print(f"⚠ GRADE_LIMIT ({GRADE_LIMIT}件) に達したため処理を打ち切ります\n")
            limit_reached = True
            break

    # ---- 課題ごとの集計CSVを保存 ----
    if not summary_rows:
        print(f"（課題「{assignment_name}」: 出力対象の行がないためCSVは作成しません）\n")
        return limit_reached

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = assignment_name.replace("/", "_").replace(" ", "_")
    summary_csv = SUMMARY_DIR / f"summary_{safe_name}_{timestamp}.csv"
    with open(summary_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDNAMES)
        writer.writeheader()
        writer.writerows(summary_rows)
    print(f"集計CSV: {summary_csv}\n")

    return limit_reached


def main():
    # 採点対象の課題リストを決定
    if GRADE_ALL_ASSIGNMENTS:
        assignments = list(ASSIGNMENT_CONFIG.keys())
    else:
        assignments = [TARGET_ASSIGNMENT]

    print(f"=" * 70)
    print(f"DEBUG: MODEL_NAME = {MODEL_NAME}")
    print(f"DEBUG: API_BASE_URL = {API_BASE_URL}")
    if GRADE_ALL_ASSIGNMENTS:
        print(f"採点対象課題: 全{len(assignments)}課題（GRADE_ALL_ASSIGNMENTS=ON）")
    else:
        print(f"採点対象課題: {TARGET_ASSIGNMENT}")
    print(f"採点済みスキップ: {'ON（成功済みはスキップ・エラーは再採点）' if SKIP_ALREADY_GRADED else 'OFF（全員を再採点・上書き）'}")
    print(f"集計CSV: {'今回採点した行のみ' if SUMMARY_REGRADED_ONLY else '全学生の行'}")
    if GRADE_LIMIT is not None:
        print(f"採点上限: {GRADE_LIMIT}件（デバッグモード・全課題合計）")
    else:
        print(f"採点上限: なし（全員採点）")
    print(f"=" * 70)

    # 集計用カウンタ（全課題で共有）
    stats = {"graded": 0, "skipped": 0, "no_submission": 0, "error": 0}

    client = OpenAI(base_url=API_BASE_URL, api_key=API_KEY, timeout=REQUEST_TIMEOUT)

    for assignment_name in assignments:
        limit_reached = grade_one_assignment(client, assignment_name, stats)
        if limit_reached:
            break

    # ---- サマリ表示 ----
    print("=" * 70)
    print("処理結果（全課題合計）")
    print("=" * 70)
    print(f"  採点完了: {stats['graded']} 件")
    print(f"  採点済みスキップ: {stats['skipped']} 件")
    print(f"  未提出: {stats['no_submission']} 件")
    print(f"  エラー: {stats['error']} 件")


if __name__ == "__main__":
    main()