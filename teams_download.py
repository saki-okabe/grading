from pathlib import Path

from playwright.sync_api import sync_playwright


ASSIGNMENT_NAME = "第1回課題（最終）発想法"
OUTPUT_ROOT = Path("Submitted files")


with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")

    teams_page = next(
        page
        for context in browser.contexts
        for page in context.pages
        if "teams.cloud.microsoft" in page.url
    )

    assignment_frame = next(
        frame
        for frame in teams_page.frames
        if frame.get_by_text("学生の作業", exact=True).count() > 0
    )

    processed_students = set()
    downloaded_count = 0
    existing_count = 0
    non_pdf_count = 0
    failed_count = 0

    while True:
        student_button = assignment_frame.locator(
            "button[aria-label^='現在選択されている受講者:']"
        )

        student_button.wait_for(state="visible", timeout=30000)
        student_name = student_button.get_attribute("title")

        if not student_name:
            print("学生名を取得できませんでした")
            break

        if student_name in processed_students:
            print("処理済みの学生に戻ったため終了します")
            break

        processed_students.add(student_name)
        print(f"\n処理中: {student_name}")

        version_dir = (
            OUTPUT_ROOT
            / student_name
            / ASSIGNMENT_NAME
            / "バージョン 1"
        )
        version_dir.mkdir(parents=True, exist_ok=True)

        options_buttons = assignment_frame.get_by_title(
            "その他の添付ファイル オプション",
            exact=True
        )

        try:
            options_buttons.first.wait_for(
                state="visible",
                timeout=30000
            )
            attachment_count = options_buttons.count()

        except Exception:
            print("  提出ファイルなし")
            attachment_count = 0

        for attachment_number in range(attachment_count):
            try:
                options_button = assignment_frame.get_by_title(
                    "その他の添付ファイル オプション",
                    exact=True
                ).nth(attachment_number)

                file_button = options_button.locator(
                    "xpath=preceding::button[@title][1]"
                )
                expected_name = file_button.get_attribute("title")

                if not expected_name:
                    print("  ファイル名を取得できないためスキップ")
                    non_pdf_count += 1
                    continue

                if Path(expected_name).suffix.lower() != ".pdf":
                    print(f"  PDF以外をスキップ: {expected_name}")
                    non_pdf_count += 1
                    continue

                expected_path = version_dir / expected_name

                if expected_path.exists():
                    print(f"  取得済み: {expected_name}")
                    existing_count += 1
                    continue

                download = None
                last_error = None

                for attempt in range(1, 4):
                    try:
                        # Teamsによる画面更新に備えて毎回取得する
                        options_button = assignment_frame.get_by_title(
                            "その他の添付ファイル オプション",
                            exact=True
                        ).nth(attachment_number)

                        options_button.wait_for(
                            state="visible",
                            timeout=30000
                        )
                        options_button.click()

                        # 文字部分ではなくメニュー項目全体を取得する
                        download_menu = assignment_frame.locator(
                            "[role='menuitem']"
                        ).filter(
                            has_text="ダウンロード"
                        ).last

                        download_menu.wait_for(
                            state="visible",
                            timeout=10000
                        )

                        # Teamsのメニュー表示が安定するまで待つ
                        teams_page.wait_for_timeout(700)

                        with teams_page.expect_download(
                            timeout=60000
                        ) as download_info:
                            download_menu.click(force=True)

                        download = download_info.value
                        break

                    except Exception as error:
                        last_error = error
                        print(f"  再試行 {attempt}/3")

                        teams_page.keyboard.press("Escape")
                        teams_page.wait_for_timeout(1500)

                if download is None:
                    raise last_error

                downloaded_name = download.suggested_filename

                # 念のため、実際のダウンロード名もPDFか確認
                if Path(downloaded_name).suffix.lower() != ".pdf":
                    print(
                        f"  ダウンロード結果がPDF以外のため保存しません: "
                        f"{downloaded_name}"
                    )
                    non_pdf_count += 1
                    continue

                destination = version_dir / downloaded_name
                download.save_as(destination)

                print(f"  保存: {destination.name}")
                downloaded_count += 1

            except Exception as error:
                print(f"  ダウンロード失敗: {error}")
                failed_count += 1

                teams_page.keyboard.press("Escape")
                teams_page.wait_for_timeout(1000)

        next_button = assignment_frame.locator(
            "button[aria-label^='次の学生に移動:']"
        )

        if next_button.count() == 0 or next_button.is_disabled():
            print("\n最後の学生まで完了しました")
            break

        previous_student = student_name
        next_button.click()

        try:
            for _ in range(60):
                teams_page.wait_for_timeout(500)

                current_name = student_button.get_attribute("title")

                if current_name != previous_student:
                    break
            else:
                print("次の学生の読み込みが終わりませんでした")
                break

        except Exception as error:
            print("次の学生への移動に失敗しました:", error)
            break

    print("\n処理結果")
    print("学生数:", len(processed_students))
    print("新規PDF:", downloaded_count)
    print("取得済みPDF:", existing_count)
    print("PDF以外:", non_pdf_count)
    print("失敗:", failed_count)