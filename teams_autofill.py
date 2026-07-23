"""
Teams（Web）の採点欄に、Teams採点用CSVの点数を自動入力するスクリプト。

概要:
    add_teams_columns.py が出力した「Teams採点用CSV」（1課題1ファイル）を読み、
    Teamsの成績入力ページの各入力欄に点数を自動入力する。
    各入力欄の aria-label から学生名を読み取ってCSVと照合するため、
    ↓キーの順送りに頼らず「別人に点数を入れる」事故を防ぐ。

前提（すべてWindows上で操作する想定。実行Pythonもpyenv-win）:
    1. Chromeをリモートデバッグ有効で起動する（既存のChromeは一度閉じる）:
         "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" ^
             --remote-debugging-port=9222 --user-data-dir="C:\\chrome-teams-profile"
    2. そのChromeで手動でTeamsにログインし、目的の課題の「成績」ページを開く。
    3. Playwrightを導入する（CDP接続のみなので playwright install は不要）:
         pip install playwright

使い方:
    1. 下の CSV_PATH を対象課題のTeams採点用CSVに合わせる。
    2. まず DRY_RUN = True のまま実行し、突き合わせ結果（誰に何点入るか）を確認する:
         python teams_autofill.py
    3. 問題なければ DRY_RUN = False にして本番入力する。

想定するHTML:
    点数欄:   <input data-test="points-input" aria-label="YAMADA, Taro の 9 点中の成績" ...>
    課題名:   <h1 data-test="assignment-title">リフレクションシート7</h1>
"""
import csv
import re
import unicodedata
from pathlib import Path

from playwright.sync_api import sync_playwright

# ============================================================
# 設定
# ============================================================
# Teams採点用CSV（add_teams_columns.py の出力。1課題1ファイル）
# 単一課題モード（BATCH_MODE=False）で入力する1ファイル
CSV_PATH = Path("./result_summaries_teams/summary_事前課題5_20260705_203101.csv")

# 半自動バッチモードで参照する、Teams採点用CSVが入っているフォルダ
TEAMS_CSV_DIR = Path("./result_summaries_teams")

# リモートデバッグ中のChromeへの接続先
CDP_URL = "http://localhost:9222"

# 要素セレクタ（検証ツールで確認済みの安定した data-test 属性を使う）
ASSIGNMENT_TITLE_SELECTOR = '[data-test="assignment-title"]'
GRADE_INPUT_SELECTOR = '[data-test="points-input"]'

# 実行モード
# BATCH_MODE = True : 半自動。起動しっぱなしで、あなたはTeamsで課題ページを切り替えるだけ。
#                     開いている課題のタイトルを検知し、TEAMS_CSV_DIR から該当CSVを
#                     自動選択して入力 → Enterで次の課題へ、を繰り返す（21課題向け）。
# BATCH_MODE = False: 従来どおり CSV_PATH の1課題だけを入力する。
BATCH_MODE = True
DRY_RUN = False           # True: 実際には入力せず、突き合わせ結果だけ表示する
SKIP_IF_FILLED = True     # True: 既に値が入っている入力欄はスキップする
FORCE_ASSIGNMENT = False  # True: 課題名が一致しなくても中断せず続行する（単一課題モード時）

# Teams側の氏名の綴りミス・登録ミスに対する手動対応表。
# 自動照合（完全一致・順序ゆれ）で拾えない学生を、明示的にCSVの英語名へ結びつける。
#   キー: Teams側の氏名（aria-labelの名前部分。カンマ・全角半角・大文字小文字は無視して照合）
#   値  : CSVの「英語名」列の値（そのまま）
# 例: Teamsで "SUZKI, Hanako"（U抜け）→ CSVの "SUZUKI Hanako" に対応させる。
# ※同じ学生は全課題で同じ綴りになるため、一度書けば全課題で有効。
# 自分の授業の学生名に合わせて記入する（不要なら空 {} のままでよい）。
NAME_OVERRIDES = {
    # "SUZKI, Hanako": "SUZUKI Hanako",
}

# 入力後に次の学生の欄へ移動するキー（↓で移動できることを確認済み）
NAV_KEY = "ArrowDown"

# --- 入力速度・待機（入力ラグ対策。遅いと感じたら数値を上げる） ---
# 1文字あたりのタイピング間隔（ミリ秒）。大きいほどゆっくり打つ
TYPE_DELAY_MS = 90
# 点数を入力し終えてから↓で移動するまでの待機（ミリ秒）。値の確定を待つ
AFTER_TYPE_WAIT_MS = 200
# ↓移動などキー操作後の待機（ミリ秒）。仮想リストの再描画・フォーカス移動を待つ
STEP_WAIT_MS = 250

# 安全のための最大反復数（無限ループ防止）
MAX_ROWS = 1000

# aria-label から「氏名」と「満点」を取り出す正規表現
#   例: "YAMADA, Taro の 9 点中の成績" → name="YAMADA, Taro", max="9"
ARIA_LABEL_RE = re.compile(r"^(?P<name>.+?)\s*の\s*(?P<max>\d+)\s*点中")


# ============================================================
# 文字列の正規化・照合
# ============================================================
def normalize_name(name: str) -> str:
    """氏名照合用に正規化する。

    - NFKCで全角/半角を統一
    - カンマ・ピリオドを空白に置換（"YAMADA, Taro" → "YAMADA Taro"）
    - 連続空白を1つにまとめ、前後空白を除去
    - 大文字小文字を無視（casefold）
    """
    s = unicodedata.normalize("NFKC", name or "")
    s = s.replace(",", " ").replace(".", " ")
    s = " ".join(s.split())
    return s.casefold()


def token_key(name: str) -> tuple[str, ...]:
    """氏名を語の集合（順序無視）に正規化する。

    姓名の順序が逆に登録されている学生（例: CSV "YAMADA Taro" に対し
    Teams側が "TARO, Yamada"）を、完全一致が外れたときのフォールバックで
    拾うために使う。
    """
    return tuple(sorted(normalize_name(name).split()))


def normalize_title(title: str) -> str:
    """課題名照合用に正規化する（NFKC・小文字化し、空白とアンダースコアを全除去）。

    集計ファイル名は空白を "_" に置換して作られるため、ファイル名由来のCSVの課題名は
    "事前課題2_最終版" のように "_" を含む一方、ページ側は "事前課題2 最終版"（空白）。
    照合ではこの差を吸収するため、空白・アンダースコアをまとめて無視する。
    """
    s = unicodedata.normalize("NFKC", title or "")
    s = s.replace("_", " ")
    s = "".join(s.split())
    return s.casefold()


def titles_match(page_title: str, csv_title: str) -> bool:
    """ページの課題タイトルとCSVの課題名が一致するか判定する。

    「事前課題1 最終版」(CSV) と「事前課題1」(ページ) のように片方が
    もう片方の一部になっているケースも一致とみなす。
    """
    a = normalize_title(page_title)
    b = normalize_title(csv_title)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def parse_score(row: dict) -> str | None:
    """CSVの1行から入力すべき点数文字列を返す。空欄・非数値なら None。

    0点はそのまま入力対象（未提出=0点）。空欄（採点エラー等）はスキップ。
    """
    raw = (row.get("点数") or "").strip()
    if raw == "":
        return None
    try:
        float(raw)  # "0" や "8" などの数値のみ受け付ける（"?" 等は除外）
    except ValueError:
        return None
    return raw


# ============================================================
# CSV読み込み
# ============================================================
def load_csv(path: Path) -> list[dict]:
    """Teams採点用CSVを読み込む（BOM付きUTF-8）。"""
    if not path.exists():
        raise FileNotFoundError(f"CSVが見つかりません: {path}")
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def build_lookup(rows: list[dict]) -> tuple[dict[str, dict], set[str], dict[tuple, list]]:
    """照合用の索引を作る。

    Returns:
        lookup      : 英語名（正規化）→ 行。完全一致用。
        ambiguous   : 完全一致キーが重複して一意に定まらないキー集合。
        token_index : 語の集合（順序無視）→ 該当行のリスト。順序ゆれのフォールバック用。
    """
    lookup: dict[str, dict] = {}
    ambiguous: set[str] = set()
    token_index: dict[tuple, list] = {}
    for row in rows:
        eng = (row.get("英語名") or "").strip()
        if not eng:
            continue
        key = normalize_name(eng)
        if key in lookup:
            ambiguous.add(key)
        else:
            lookup[key] = row
        token_index.setdefault(token_key(eng), []).append(row)
    return lookup, ambiguous, token_index


def build_overrides() -> dict[str, str]:
    """NAME_OVERRIDES を正規化した「Teams氏名(正規化) → CSV英語名(正規化)」に変換する。"""
    return {normalize_name(k): normalize_name(v) for k, v in NAME_OVERRIDES.items()}


# ============================================================
# Playwright: ページ・フレーム探索
# ============================================================
def find_grade_frame(browser):
    """点数入力欄を含む (page, frame) を全タブ・全フレームから探す。

    Teamsの採点UIがiframe内にある場合にも対応するためフレーム単位で探索する。
    """
    for context in browser.contexts:
        for page in context.pages:
            for frame in page.frames:
                try:
                    if frame.locator(GRADE_INPUT_SELECTOR).count() > 0:
                        return page, frame
                except Exception:
                    continue
    return None, None


def get_assignment_title(page) -> str:
    """ページ内（全フレーム）から課題タイトルのテキストを取得する。"""
    for frame in page.frames:
        try:
            loc = frame.locator(ASSIGNMENT_TITLE_SELECTOR)
            if loc.count() > 0:
                return (loc.first.inner_text() or "").strip()
        except Exception:
            continue
    return ""


# ============================================================
# 入力ループ
# ============================================================
def run_fill(page, frame, lookup: dict[str, dict], ambiguous: set[str],
             token_index: dict[tuple, list], overrides: dict[str, str]) -> None:
    """フォーカスした入力欄の aria-label を読みながら点数を入力し、↓で次へ進む。"""
    inputs = frame.locator(GRADE_INPUT_SELECTOR)
    if inputs.count() == 0:
        print("❌ 点数入力欄が見つかりません。")
        return

    # 先頭の欄をフォーカス
    first = inputs.first
    first.scroll_into_view_if_needed()
    first.click()
    page.wait_for_timeout(STEP_WAIT_MS)

    stats = {
        "filled": 0, "skip_filled": 0, "skip_no_csv": 0,
        "skip_empty": 0, "skip_ambiguous": 0, "skip_parse": 0,
    }
    matched_keys: set[str] = set()
    seen_ids: set[str] = set()

    for _ in range(MAX_ROWS):
        info = frame.evaluate(
            """() => {
                const el = document.activeElement;
                if (!el) return null;
                return {
                    id: el.id,
                    aria: el.getAttribute('aria-label'),
                    value: el.value,
                    test: el.getAttribute('data-test'),
                };
            }"""
        )
        # フォーカスが点数欄から外れたら終了
        if not info or info.get("test") != "points-input":
            break

        el_id = info.get("id") or ""
        if el_id and el_id in seen_ids:
            break  # 同じ欄に戻った/これ以上進まない → 末尾
        if el_id:
            seen_ids.add(el_id)

        aria = info.get("aria") or ""
        m = ARIA_LABEL_RE.match(unicodedata.normalize("NFKC", aria))
        if not m:
            print(f"  [skip] aria-label解析不可: {aria!r}")
            stats["skip_parse"] += 1
            _press_next(page)
            continue

        raw_name = m.group("name").strip()
        key = normalize_name(raw_name)

        if key in ambiguous:
            print(f"  [skip] 英語名が重複し一意に定まらない: {raw_name!r}")
            stats["skip_ambiguous"] += 1
            _press_next(page)
            continue

        row = lookup.get(key)
        match_note = ""
        if row is None and key in overrides:
            # 手動対応表（綴りミス・登録ミス）で明示的にCSVの英語名へ結びつける
            row = lookup.get(overrides[key])
            if row is not None:
                match_note = f"  ※手動対応表で対応（CSV英語名={row.get('英語名')!r}）"
            else:
                print(f"  [skip] 手動対応表の対応先がCSVに見つかりません: {raw_name!r} → {overrides[key]!r}")
                stats["skip_no_csv"] += 1
                _press_next(page)
                continue
        if row is None:
            # フォールバック: 姓名の順序ゆれ（語の集合が一致するか）
            cands = token_index.get(token_key(raw_name), [])
            uniq = {normalize_name(r.get("英語名") or ""): r for r in cands}
            if len(uniq) == 1:
                row = next(iter(uniq.values()))
                match_note = f"  ※名前順ゆれをtoken一致で対応（CSV英語名={row.get('英語名')!r}）"
            elif len(uniq) >= 2:
                print(f"  [skip] token一致の候補が複数: {raw_name!r} → {list(uniq.keys())}")
                stats["skip_ambiguous"] += 1
                _press_next(page)
                continue

        if row is None:
            print(f"  [skip] CSVに該当なし: {raw_name!r} (key={key!r})")
            stats["skip_no_csv"] += 1
            _press_next(page)
            continue

        matched_keys.add(normalize_name(row.get("英語名") or ""))
        score = parse_score(row)
        if score is None:
            print(f"  [skip] 点数が空/非数値: {raw_name!r} 状態={row.get('状態')!r}")
            stats["skip_empty"] += 1
            _press_next(page)
            continue

        current = (info.get("value") or "").strip()
        if SKIP_IF_FILLED and current != "":
            print(f"  [skip] 既に入力済み({current}): {raw_name!r}")
            stats["skip_filled"] += 1
            _press_next(page)
            continue

        if DRY_RUN:
            print(f"  [dry ] {raw_name!r} ← {score}  (現在値={current!r}){match_note}")
        else:
            # フォーカス中の欄を全選択して上書き入力（React制御inputに確実に反映）
            page.keyboard.press("Control+A")
            page.keyboard.type(str(score), delay=TYPE_DELAY_MS)
            # 入力値が確定するのを待ってから次へ移動する
            page.wait_for_timeout(AFTER_TYPE_WAIT_MS)
            print(f"  [fill] {raw_name!r} ← {score}{match_note}")
        stats["filled"] += 1
        _press_next(page)

    # ---- 集計 ----
    print("\n" + "=" * 60)
    print("処理結果" + ("（DRY_RUN: 実際には入力していません）" if DRY_RUN else ""))
    print("=" * 60)
    print(f"  入力{'予定' if DRY_RUN else '完了'}: {stats['filled']} 件")
    print(f"  スキップ（既に入力済み）  : {stats['skip_filled']} 件")
    print(f"  スキップ（点数が空/非数値）: {stats['skip_empty']} 件")
    print(f"  スキップ（CSVに該当なし）  : {stats['skip_no_csv']} 件")
    print(f"  スキップ（英語名が重複）  : {stats['skip_ambiguous']} 件")
    print(f"  スキップ（ラベル解析不可）: {stats['skip_parse']} 件")

    unmatched = [row for key, row in lookup.items() if key not in matched_keys]
    if unmatched:
        print(f"\n⚠ CSVにあるが画面に現れなかった学生: {len(unmatched)} 名")
        for row in unmatched:
            print(f"    - {row.get('英語名')} / {row.get('氏名')} (点数={row.get('点数')})")


def _press_next(page) -> None:
    """↓キーで次の学生の入力欄へ移動する。"""
    page.keyboard.press(NAV_KEY)
    page.wait_for_timeout(STEP_WAIT_MS)


# ============================================================
# 課題→CSVの自動選択（半自動バッチモード用）
# ============================================================
def build_csv_index(folder: Path) -> dict[str, list[tuple]]:
    """フォルダ内の全Teams採点用CSVを読み、課題名(正規化) → [(path, 課題名, rows)] の索引を作る。"""
    if not folder.exists():
        raise FileNotFoundError(f"CSVフォルダが見つかりません: {folder}")
    index: dict[str, list[tuple]] = {}
    for path in sorted(folder.glob("summary_*.csv")):
        rows = load_csv(path)
        title = next((r["課題名"] for r in rows if r.get("課題名")), "")
        index.setdefault(normalize_title(title), []).append((path, title, rows))
    return index


def find_csvs_for_title(page_title: str, index: dict[str, list[tuple]]) -> list[tuple]:
    """ページの課題タイトルに対応するCSV候補を返す。完全一致優先、無ければ部分一致。"""
    nt = normalize_title(page_title)
    if not nt:
        return []
    if nt in index:
        return index[nt]  # 完全一致
    cands: list[tuple] = []
    for key, items in index.items():
        if key and (key in nt or nt in key):
            cands += items
    return cands


def fill_assignment(page, frame, rows: list[dict], overrides: dict[str, str]) -> None:
    """1課題分の行データから索引を作り、点数を入力する。"""
    lookup, ambiguous, token_index = build_lookup(rows)
    if ambiguous:
        print(f"  ⚠ 英語名が重複して一意に定まらないキー: {len(ambiguous)} 件（該当学生はスキップ）")
    run_fill(page, frame, lookup, ambiguous, token_index, overrides)


# ============================================================
# メイン処理
# ============================================================
def run_single(browser, overrides: dict[str, str]) -> None:
    """単一課題モード: CSV_PATH の1課題だけを入力する。"""
    rows = load_csv(CSV_PATH)
    csv_assignment = next((r["課題名"] for r in rows if r.get("課題名")), "")
    print("=" * 60)
    print(f"CSV: {CSV_PATH.name}")
    print(f"  課題名: {csv_assignment!r} / 学生 {len(rows)} 行")
    print(f"  モード: {'DRY_RUN（確認のみ）' if DRY_RUN else '本番入力'}")
    print("=" * 60)

    page, frame = find_grade_frame(browser)
    if page is None:
        print("❌ 点数入力欄のあるTeamsページが見つかりません。")
        print("   Chromeで対象課題の『成績』ページを開いているか確認してください。")
        return

    page_title = get_assignment_title(page)
    print(f"ページの課題タイトル: {page_title!r}")
    if titles_match(page_title, csv_assignment):
        print("→ 課題名OK（CSVと一致）\n")
    else:
        print(f"❌ 課題名が一致しません（ページ: {page_title!r} / CSV: {csv_assignment!r}）")
        if not FORCE_ASSIGNMENT:
            print("   中断します。ページの課題が正しい場合は FORCE_ASSIGNMENT=True で続行できます。")
            return
        print("   FORCE_ASSIGNMENT=True のため続行します。\n")

    fill_assignment(page, frame, rows, overrides)


def run_batch(browser, overrides: dict[str, str]) -> None:
    """半自動バッチモード: 開いている課題を検知し該当CSVを自動選択して入力、Enterで次へ。"""
    index = build_csv_index(TEAMS_CSV_DIR)
    print("=" * 60)
    print(f"半自動バッチモード（{'DRY_RUN・確認のみ' if DRY_RUN else '本番入力'}）")
    print(f"  CSVフォルダ: {TEAMS_CSV_DIR}  （{sum(len(v) for v in index.values())} ファイル）")
    print("  Teamsで採点したい課題の『成績』ページを開いてから Enter を押してください。")
    print("  以降、課題ページを切り替えるたびに Enter。q + Enter で終了。")
    print("=" * 60)

    done_titles: set[str] = set()
    while True:
        ans = input("\n採点する課題ページを開いたら Enter（q + Enter で終了）> ").strip().lower()
        if ans == "q":
            print("終了します。")
            break

        page, frame = find_grade_frame(browser)
        if page is None:
            print("❌ 点数入力欄のあるページが見つかりません。課題の『成績』ページを開いていますか？")
            continue

        title = get_assignment_title(page)
        if not title:
            print("⚠ 課題タイトルを取得できませんでした。ページを確認してください。")
            continue

        cands = find_csvs_for_title(title, index)
        if len(cands) == 0:
            print(f"⚠ 課題『{title}』に対応するCSVが {TEAMS_CSV_DIR} に見つかりません。スキップします。")
            continue
        if len(cands) > 1:
            names = [c[0].name for c in cands]
            print(f"⚠ 課題『{title}』に複数CSVが該当（{names}）。安全のためスキップします。")
            continue

        path, ctitle, rows = cands[0]
        nt = normalize_title(title)
        if nt in done_titles:
            print(f"（課題『{title}』はこのセッションで既に処理済みです。再度入力します）")
        done_titles.add(nt)

        print(f"\n▼ 課題『{title}』 → CSV: {path.name}")
        fill_assignment(page, frame, rows, overrides)


def main() -> None:
    overrides = build_overrides()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        if BATCH_MODE:
            run_batch(browser, overrides)
        else:
            run_single(browser, overrides)


if __name__ == "__main__":
    main()
