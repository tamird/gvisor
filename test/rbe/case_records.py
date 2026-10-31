"""Reconcile the named Go tests, including test2json's SYN-prefixed records."""
import re


def case_records(raw, expected):
    log = raw.decode().replace("\x16", "")
    ran = re.findall(r"^\s*=== RUN\s+(\S+)", log, re.MULTILINE)
    passed = re.findall(r"^\s*--- PASS: (\S+)", log, re.MULTILINE)
    failed = re.findall(r"^\s*--- FAIL: (\S+)", log, re.MULTILINE)
    skipped = re.findall(r"^\s*--- SKIP: (\S+)", log, re.MULTILINE)
    expected = sorted(expected)
    ok = expected and sorted(ran) == expected == sorted(passed) and not failed and not skipped
    return dict(expected=expected, executed=ran, passed=passed, failed=failed, skipped=skipped,
                missing=sorted(set(expected) - set(ran)), verified=bool(ok))
