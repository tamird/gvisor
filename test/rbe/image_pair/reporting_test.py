"""Exercise rejection boundaries using the actual six-row functional report."""
from pathlib import Path
import unittest

import run


class ReportingTest(unittest.TestCase):
    def setUp(self):
        self.text = (Path(__file__).parent / "reporting_fixture.txt").read_text()
        self.expected = {
            f"BenchmarkHttpd/filesize.{size}/concurrency.{concurrency}": {
                "iterations": 100,
                "units": ["ns/op", "average_latency.s", "requests_per_second.QPS"],
            }
            for size in ("1Kb", "1Mb", "notfound") for concurrency in (1, 25)
        }

    def test_native_report(self):
        _, values = run.measurements(self.text, self.expected)
        self.assertEqual({row["name"] for row in values}, set(self.expected))

    def test_missing_and_duplicate_rows(self):
        rows = self.text.splitlines()
        for text in ("\n".join(rows[1:]), "\n".join([rows[0], *rows])):
            with self.subTest(text=text), self.assertRaises(ValueError):
                run.measurements(text, self.expected)

    def test_failed_and_late_rows(self):
        for text in (self.text + "FAIL\n", self.text + self.text.splitlines()[0] + "\n",
                     self.text.replace("PASS", ""), self.text + "PASS\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                run.measurements(text, self.expected)

    def test_wrong_iterations_and_metrics(self):
        for text in (self.text.replace("100", "99", 1),
                     self.text.replace("3717753 ns/op", "nan ns/op", 1),
                     self.text.replace("ns/op", "unknown/unit", 1)):
            with self.subTest(text=text), self.assertRaises(ValueError):
                run.measurements(text, self.expected)

    def test_balanced_fixed_cohort(self):
        rows = run.cohort("100x")
        self.assertEqual(len(rows), 34)
        self.assertFalse(any(row.measured for row in rows[:2]))
        for block in range(16):
            expected = ["before", "after"] if block % 2 == 0 else ["after", "before"]
            self.assertEqual([row.side for row in rows[2 + 2 * block:4 + 2 * block]], expected)


if __name__ == "__main__":
    unittest.main()
