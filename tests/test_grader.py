"""架空データだけで採点の既存契約を検証する。外部APIは呼び出さない。"""
import csv
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

import grader


class GraderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.submitted = self.root / "submitted"
        self.submitted.mkdir()
        self.summary = self.root / "summary"
        self.summary.mkdir()
        self.roster = self.root / "roster.csv"
        self.roster.write_text("TEST A,テストA\nTEST B,テストB\nTEST C,テストC\n", encoding="utf-8")
        settings = patch.multiple(
            grader, SUBMITTED_DIR=self.submitted, STUDENT_LIST_PATH=self.roster,
            SUMMARY_DIR=self.summary, ASSIGNMENT_CONFIG={"テスト課題": "landscape"},
            MODEL_NAME="test-model", SKIP_ALREADY_GRADED=False,
            SUMMARY_REGRADED_ONLY=False, GRADE_LIMIT=None,
        )
        settings.start()
        self.addCleanup(settings.stop)

    def submission(self, name="テストA", version=1):
        folder = self.submitted / name / "テスト課題" / f"バージョン {version}"
        folder.mkdir(parents=True)
        image = folder / "sample.png"
        Image.new("RGB", (10, 20), "white").save(image)
        return image

    def test_latest_version_and_pdf_preference(self):
        old = self.submission(version=2)
        latest = self.submission(version=10)
        pdf = latest.with_suffix(".pdf")
        pdf.touch()
        self.assertEqual(grader.find_latest_submission(old.parent.parent), pdf)
        pdf.unlink()
        self.assertEqual(grader.find_latest_submission(old.parent.parent), latest)
        latest.unlink()
        self.assertIsNone(grader.find_latest_submission(old.parent.parent))
        self.assertIsNone(grader.find_latest_submission(self.root / "missing"))

    def test_roster_order_missing_student_and_missing_assignment(self):
        self.submission("テストB")
        self.submission("名簿外")
        (self.submitted / "テストA").mkdir()
        students = grader.collect_students("テスト課題")
        self.assertEqual([s["name"] for s in students], ["テストA", "テストB"])
        self.assertIsNone(students[0]["submission"])
        self.assertEqual(students[1]["english_name"], "TEST B")

    def test_roster_is_required(self):
        self.roster.unlink()
        with self.assertRaises(FileNotFoundError):
            grader.collect_students("テスト課題")

    def test_image_rotation_merge_and_pdf_conversion(self):
        image = self.submission()
        self.assertEqual(grader.load_images(image, "テスト課題")[0].size, (20, 10))
        with patch.object(grader, "convert_from_path", return_value=[Image.new("RGB", (10, 20))]) as convert:
            self.assertEqual(grader.load_images(image.with_suffix(".pdf"), "テスト課題")[0].size, (20, 10))
            convert.assert_called_once_with(image.with_suffix(".pdf"), dpi=200)
        merged = grader.merge_images_vertically([Image.new("RGB", (20, 10)), Image.new("RGB", (10, 20))])
        self.assertEqual(merged.size, (20, 30))
        with self.assertRaises(KeyError):
            grader.load_images(image, "未登録")

    def test_parse_model_response_and_reasoning_fallback(self):
        result = {"点数": 5, "評価理由": '引用 " と {中括弧}'}
        for raw in [json.dumps(result), "前文\n```json\n" + json.dumps(result) + "\n```\n後文"]:
            self.assertEqual(grader.parse_grading_result(raw), result)
        self.assertIn("error", grader.parse_grading_result(""))
        self.assertIn("error", grader.parse_grading_result('{"点数": invalid}'))
        self.assertEqual(grader.parse_grading_result('{"点数":'), {})
        self.assertEqual(grader.extract_text_from_response(SimpleNamespace(content="", reasoning_content="fallback")), "fallback")

    def test_vlm_retry_keeps_request_contract(self):
        client = Mock()
        client.chat.completions.create.side_effect = [
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"点数":'), finish_reason="length")]),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"点数": 5}'), finish_reason="stop")]),
        ]
        result = grader.grade_image(client, Image.new("RGB", (10, 10)), "test criteria")
        self.assertEqual(result["点数"], 5)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], "test-model")
        self.assertEqual(request["messages"][0]["content"][1]["text"], "test criteria")

    def test_result_files_success_error_and_corrupt_json(self):
        image = self.submission()
        folder = image.parent.parent
        result = {"点数": 5, "評価理由": "テスト", "フィードバック": "確認"}
        grader.save_result_files(folder, "テストA", image, result, "テスト課題")
        saved = json.loads((folder / grader.RESULT_JSON_NAME).read_text(encoding="utf-8"))
        self.assertEqual(saved["採点結果"], result)
        self.assertEqual(saved["課題"], "テスト課題")
        self.assertIn("5 点", (folder / grader.RESULT_MD_NAME).read_text(encoding="utf-8"))
        self.assertTrue(grader.is_already_graded(folder))
        grader.save_result_files(folder, "テストA", image, {"error": "test error"}, "テスト課題")
        self.assertFalse(grader.is_already_graded(folder))
        (folder / grader.RESULT_JSON_NAME).write_text("{", encoding="utf-8")
        self.assertFalse(grader.is_already_graded(folder))

    def run_assignment(self):
        stats = {"graded": 0, "skipped": 0, "no_submission": 0, "error": 0}
        with patch.object(grader, "load_criteria", return_value="test criteria"), patch.object(
            grader, "grade_image", return_value={"点数": 5, "評価理由": "テスト", "フィードバック": "確認"}
        ) as grade:
            limited = grader.grade_one_assignment(Mock(), "テスト課題", stats)
        return stats, limited, grade.call_count

    def summary_rows(self):
        path = next(self.summary.glob("*.csv"))
        self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(reader.fieldnames, grader.SUMMARY_FIELDNAMES)
            return list(reader)

    def test_assignment_summary_and_skip(self):
        self.submission()
        (self.submitted / "テストB").mkdir()
        stats, limited, calls = self.run_assignment()
        self.assertEqual((stats["graded"], stats["no_submission"], limited, calls), (1, 1, False, 1))
        self.assertEqual([r["状態"] for r in self.summary_rows()], ["採点完了", "未提出"])
        with patch.object(grader, "SKIP_ALREADY_GRADED", True):
            stats, _, calls = self.run_assignment()
        self.assertEqual((stats["skipped"], calls), (1, 0))

    def test_limit_and_regraded_only(self):
        (self.submitted / "テストA").mkdir()
        self.submission("テストB")
        self.submission("テストC")
        with patch.multiple(grader, GRADE_LIMIT=1, SUMMARY_REGRADED_ONLY=True):
            stats, limited, calls = self.run_assignment()
        self.assertEqual((stats["graded"], stats["no_submission"], limited, calls), (1, 1, True, 1))
        self.assertEqual([r["氏名"] for r in self.summary_rows()], ["テストB"])

    def test_error_is_retried_and_recorded(self):
        image = self.submission()
        grader.save_result_files(image.parent.parent, "テストA", image, {"error": "old"}, "テスト課題")
        with patch.object(grader, "SKIP_ALREADY_GRADED", True):
            stats, _, calls = self.run_assignment()
        self.assertEqual((stats["graded"], calls), (1, 1))
        with patch.object(grader, "load_criteria", return_value="test"), patch.object(grader, "grade_image", side_effect=RuntimeError("mock failure")):
            stats = {"graded": 0, "skipped": 0, "no_submission": 0, "error": 0}
            grader.grade_one_assignment(Mock(), "テスト課題", stats)
        self.assertEqual(stats["error"], 1)

    def test_main_shares_limit_across_assignments(self):
        def grade(client, name, stats):
            stats["graded"] += 1
            return stats["graded"] >= 2

        with patch.object(grader, "configure_runtime") as configure, patch.multiple(
            grader, GRADE_ALL_ASSIGNMENTS=True, ASSIGNMENT_CONFIG={"first": "portrait", "second": "portrait", "third": "portrait"}
        ), patch.object(grader, "OpenAI"), patch.object(grader, "grade_one_assignment", side_effect=grade) as run:
            grader.main()
        configure.assert_called_once_with()
        self.assertEqual([call.args[1] for call in run.call_args_list], ["first", "second"])

    def test_no_summary_when_everything_is_skipped(self):
        image = self.submission()
        grader.save_result_files(image.parent.parent, "テストA", image, {"点数": 5}, "テスト課題")
        with patch.multiple(grader, SKIP_ALREADY_GRADED=True, SUMMARY_REGRADED_ONLY=True):
            stats, _, calls = self.run_assignment()
        self.assertEqual((stats["skipped"], calls), (1, 0))
        self.assertEqual(list(self.summary.iterdir()), [])


class RuntimeTests(unittest.TestCase):
    def test_import_without_env_or_repository_files_has_no_output(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {k: v for k, v in os.environ.items() if k not in {"API_BASE_URL", "API_KEY", "MODEL_NAME"}}
            env["PYTHONPATH"] = str(Path(grader.__file__).parent)
            subprocess.run([sys.executable, "-c", "import grader"], cwd=directory, env=env, check=True)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_env_settings_and_validation(self):
        # 別モジュールで読み込み、他の回帰テストのグローバル設定を汚さない。
        spec = importlib.util.spec_from_file_location("runtime_test_grader", grader.__file__)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        values = {"API_BASE_URL": "http://localhost:1234/v1", "API_KEY": "test-only", "MODEL_NAME": "test", "TARGET_ASSIGNMENT": "テスト課題", "GRADE_ALL_ASSIGNMENTS": "yes", "SKIP_ALREADY_GRADED": "true", "SUMMARY_REGRADED_ONLY": "1", "GRADE_LIMIT": "3", "SUMMARY_DIR": "local_data/output"}
        with patch.dict(os.environ, values, clear=True), patch.object(module, "load_dotenv"), patch.object(module, "_load_assignment_config", return_value={}):
            module.configure_runtime()
            self.assertEqual(module.TARGET_ASSIGNMENT, "テスト課題")
            self.assertTrue(module.GRADE_ALL_ASSIGNMENTS)
            self.assertTrue(module.SKIP_ALREADY_GRADED)
            self.assertTrue(module.SUMMARY_REGRADED_ONLY)
            self.assertEqual(module.GRADE_LIMIT, 3)
            self.assertEqual(module.SUMMARY_DIR, Path("local_data/output"))
            for invalid in ["0", "-1", "invalid"]:
                os.environ["GRADE_LIMIT"] = invalid
                with self.assertRaises(ValueError):
                    module.configure_runtime()
            os.environ["GRADE_LIMIT"] = "none"
            module.configure_runtime()
            self.assertIsNone(module.GRADE_LIMIT)
            os.environ["SKIP_ALREADY_GRADED"] = "typo"
            with self.assertRaises(ValueError):
                module.configure_runtime()
        with patch.dict(os.environ, {}, clear=True), patch.object(module, "load_dotenv"):
            with self.assertRaisesRegex(RuntimeError, "API_BASE_URL"):
                module.configure_runtime()


if __name__ == "__main__":
    unittest.main()
