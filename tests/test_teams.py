"""ブラウザを起動せず、架空の氏名・CSVでTeams補助の契約を確認する。"""
import csv
from pathlib import Path
import tempfile
import unittest

import add_teams_columns
import teams_autofill


class TeamsTests(unittest.TestCase):
    def test_name_normalization_and_duplicate_detection(self):
        rows = [{"英語名": "TEST A", "点数": "0"}, {"英語名": "Test, A", "点数": "5"}]
        lookup, ambiguous, tokens = teams_autofill.build_lookup(rows)
        self.assertIn("test a", ambiguous)
        self.assertEqual(len(tokens[teams_autofill.token_key("A TEST")]), 2)
        self.assertEqual(lookup["test a"], rows[0])
        self.assertEqual(teams_autofill.normalize_name("ＴＥＳＴ, Ａ"), "test a")
        self.assertEqual(teams_autofill.parse_score(rows[0]), "0")
        self.assertIsNone(teams_autofill.parse_score({"点数": ""}))
        self.assertIsNone(teams_autofill.parse_score({"点数": "?"}))

    def test_legacy_csv_conversion_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "summary_テスト_課題_20260101_120000.csv"
            source.write_text("氏名,状態,点数\nテストA,採点完了,0\n", encoding="utf-8-sig")
            original = source.read_bytes()
            output = root / "converted"
            self.assertEqual(add_teams_columns.convert_file(source, {"テストA": "TEST A"}, output), (1, 0))
            self.assertEqual(source.read_bytes(), original)
            with (output / source.name).open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["課題名"], "テスト_課題")
            self.assertEqual(rows[0]["英語名"], "TEST A")
            self.assertEqual(rows[0]["点数"], "0")


if __name__ == "__main__":
    unittest.main()
