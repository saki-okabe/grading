"""
既存の集計CSV（result_summaries/ 配下）に、Teams入力用の列を追加するスクリプト。

各CSVは grader.py の旧フォーマット（ヘッダー:
氏名,状態,提出ファイル,点数,評価理由,フィードバック）を前提とする。
このスクリプトは元ファイルには手を加えず、以下の2列を先頭に追加した
新しいCSVを OUTPUT_DIR に出力する。

    - 課題名 : ファイル名 summary_<課題名>_<日時>.csv から抽出
    - 英語名 : student_list.csv の「漢字氏名 → ローマ字氏名」対応から引く

使い方:
    python add_teams_columns.py
出力:
    result_summaries_teams/<元と同じファイル名>.csv
"""
import csv
import re
from pathlib import Path

# ============================================================
# 設定
# ============================================================
# 変換元CSVが入っているフォルダ
INPUT_DIR = Path("./result_summaries")

# 変換後CSVの出力先フォルダ（元ファイルは残す）
OUTPUT_DIR = Path("./result_summaries_teams")

# 学生名簿（1列目: ローマ字氏名, 2列目: 漢字氏名）
STUDENT_LIST_PATH = Path("./student_list.csv")

# 追加する識別列（この順で先頭に付ける）。氏名は元データにあるので間に挟む。
PREFIX_FIELDS = ["課題名", "氏名", "英語名"]

# ファイル名末尾のタイムスタンプ（_YYYYMMDD_HHMMSS）を取り除く正規表現
_TIMESTAMP_RE = re.compile(r"^summary_(?P<name>.+)_\d{8}_\d{6}$")


# ============================================================
# 補助関数
# ============================================================
def load_english_name_map(csv_path: Path) -> dict[str, str]:
    """student_list.csvから「漢字氏名 → ローマ字氏名」の対応を読み込む。"""
    if not csv_path.exists():
        raise FileNotFoundError(f"学生名簿が見つかりません: {csv_path}")
    mapping: dict[str, str] = {}
    with csv_path.open(encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 2 and row[1].strip():
                mapping[row[1].strip()] = row[0].strip()
    return mapping


def extract_assignment_name(csv_path: Path) -> str:
    """summary_<課題名>_<YYYYMMDD>_<HHMMSS>.csv から課題名を取り出す。

    課題名にアンダースコアや全角スペースが含まれていても、末尾の
    タイムスタンプ部分だけを確実に取り除く。想定外の名前なら stem をそのまま返す。
    """
    m = _TIMESTAMP_RE.match(csv_path.stem)
    return m.group("name") if m else csv_path.stem


def convert_file(src: Path, english_map: dict[str, str], out_dir: Path) -> tuple[int, int]:
    """1つのCSVを変換して out_dir に出力する。

    Returns:
        (行数, 英語名が引けなかった行数)
    """
    assignment_name = extract_assignment_name(src)

    # utf-8-sig で読み書きする（grader.py が BOM 付きで出力しているため）
    with src.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        original_fields = reader.fieldnames or []
        rows = list(reader)

    # 出力カラム順: 課題名, 氏名, 英語名, （残りの元カラム）
    rest_fields = [c for c in original_fields if c not in PREFIX_FIELDS]
    out_fields = PREFIX_FIELDS + rest_fields

    missing = 0
    out_rows = []
    for row in rows:
        name = (row.get("氏名") or "").strip()
        english = english_map.get(name, "")
        if name and not english:
            missing += 1
        new_row = dict(row)
        new_row["課題名"] = assignment_name
        new_row["英語名"] = english
        out_rows.append(new_row)

    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / src.name
    with dest.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(out_rows)

    return len(out_rows), missing


# ============================================================
# メイン処理
# ============================================================
def main():
    if not INPUT_DIR.exists():
        raise FileNotFoundError(f"入力フォルダが見つかりません: {INPUT_DIR}")

    english_map = load_english_name_map(STUDENT_LIST_PATH)
    print(f"学生名簿を読み込みました（{len(english_map)} 名）")

    csv_files = sorted(INPUT_DIR.glob("summary_*.csv"))
    if not csv_files:
        print(f"⚠ {INPUT_DIR} に summary_*.csv が見つかりません")
        return

    print(f"対象CSV: {len(csv_files)} 件 → 出力先: {OUTPUT_DIR}\n")

    total_rows = 0
    total_missing = 0
    for src in csv_files:
        assignment_name = extract_assignment_name(src)
        rows, missing = convert_file(src, english_map, OUTPUT_DIR)
        total_rows += rows
        total_missing += missing
        warn = f"  ⚠ 英語名なし {missing} 件" if missing else ""
        print(f"  {src.name}  → 課題名『{assignment_name}』 / {rows} 行{warn}")

    print(f"\n完了: {len(csv_files)} ファイル / {total_rows} 行を出力しました")
    if total_missing:
        print(
            f"⚠ 英語名が引けなかった行が計 {total_missing} 件あります。"
            f"（{STUDENT_LIST_PATH} に漢字氏名が無い、または表記ゆれの可能性）"
        )


if __name__ == "__main__":
    main()
