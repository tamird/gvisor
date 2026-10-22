"""Exercise native execution-log framing and bounded memory on the coordinator."""

import io
import json
import resource
import unittest

from image_execution import execution_records


class RepeatedLog(io.TextIOBase):
    """Generate a large log without first materializing it in the test."""

    def __init__(self, record: str, count: int):
        self.record = record
        self.remaining = len(record) * count
        self.offset = 0

    def read(self, size: int = -1) -> str:
        if not 0 < size <= 65536:
            raise AssertionError(f"Expected a bounded read, got {size}")
        size = min(size, self.remaining)
        repeated = self.record * ((self.offset + size) // len(self.record) + 1)
        result = repeated[self.offset:self.offset + size]
        self.offset = (self.offset + size) % len(self.record)
        self.remaining -= size
        return result


class ExecutionRecordsTest(unittest.TestCase):
    def test_exact_text_and_nested_values(self):
        values = [{"targetLabel": "//owner", "inputs": [{"path": "a}b\\\"é"}]}, {}]
        text = " \r\n" + json.dumps(values[0], ensure_ascii=False, indent=2) + json.dumps(values[1]) + "\t\r\n"
        records = list(execution_records(io.StringIO(text)))
        self.assertEqual("".join(raw for raw, _ in records), text)
        self.assertEqual([value for _, value in records if value is not None], values)

    def test_credential_crosses_read_boundary(self):
        secret = "qualified-token"
        empty = json.dumps({"padding": "", "header": "x-buildbuddy-api-key=" + secret})
        text = json.dumps({"padding": "x" * (65536 - 7 - empty.index(secret)),
                           "header": "x-buildbuddy-api-key=" + secret})
        self.assertLess(text.index(secret), 65536)
        self.assertGreater(text.index(secret) + len(secret), 65536)
        records = list(execution_records(io.StringIO(text)))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][0].replace(secret, "[REDACTED]"), text.replace(secret, "[REDACTED]"))

    def test_record_limit(self):
        text = json.dumps({"value": "é" * 128}, ensure_ascii=False)
        self.assertEqual(list(execution_records(io.StringIO(text), len(text)))[0][0], text)
        with self.assertRaisesRegex(ValueError, "character limit"):
            list(execution_records(io.StringIO(text), len(text) - 1))

    def test_invalid_records(self):
        for text in ('{"unfinished":', '{"x": 1}garbage', '[]', 'null', '1'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                list(execution_records(io.StringIO(text)))

    def test_empty_and_whitespace(self):
        self.assertEqual(list(execution_records(io.StringIO(""))), [])
        self.assertEqual(list(execution_records(io.StringIO(" \n"))), [(" \n", None)])

    def test_log_larger_than_process_memory_limit(self):
        record = json.dumps({"targetLabel": "//owner", "inputs": [], "value": "x" * 4096})
        count = 100000
        self.assertGreater(len(record) * count, 256 * 1024 * 1024)
        seen = 0
        for raw, value in execution_records(RepeatedLog(record, count)):
            self.assertEqual(raw, record)
            self.assertEqual(value["targetLabel"], "//owner")
            seen += 1
        self.assertEqual(seen, count)


if __name__ == "__main__":
    # This guard runs only via the pinned Bazel Python on the Linux coordinator.
    # Whole-log byte/text copies cannot fit, while individual records can.
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    unittest.main()
