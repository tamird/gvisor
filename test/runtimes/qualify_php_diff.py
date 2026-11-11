"""Check the two public outputs from gVisor's runtime runner."""

import gzip
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys


def main():
    runner = pathlib.Path(sys.argv[1]).absolute()
    arm = os.environ["DIFF_ARM"]
    receipt = pathlib.Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    result = {"arm": arm, "cases": {}}
    for name in ("mismatch", "match"):
        cases = ["000-reporting-qa/match.phpt"]
        if name == "mismatch":
            cases.append("000-reporting-qa/mismatch.phpt")
        command = [
            str(runner), "--lang=php", "--image=php-diff-qa", "--runtime=runc",
            f"--tests={','.join(cases)}", "--batch=50", "--timeout=3m",
        ]
        run = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=240, check=False)
        raw = run.stdout
        (receipt / f"{name}.log.gz").write_bytes(gzip.compress(raw))
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", raw.decode(errors="replace"))
        # Keep only the executed batch from the very long discovery list.
        tail = log[log.find("Running test case batch:"):] if "Running test case batch:" in log else log[-5000:]
        print(tail[-10000:], flush=True)
        batches = re.findall(r"Running test case batch: ([^\r\n]+)", log)
        assert len(batches) == 1 and sorted(batches[0].split(",")) == cases, (arm, name, batches)
        expected = [line.strip() for line in tail.splitlines() if "QA_EXPECTED_PAYLOAD" in line]
        observed = [line.strip() for line in tail.splitlines() if "QA_OBSERVED_PAYLOAD" in line]
        has_diff = "========DIFF========" in tail
        if name == "mismatch":
            assert run.returncode == 1 and "FAIL:" in tail and "Output:" in tail
            assert "FAIL PHP log mismatch control" in tail and "PASS PHP log success control" in tail
            assert re.search(r"Number of tests\s*:\s*2\b", tail)
            assert re.search(r"Tests failed\s*:\s*1\b", tail)
            assert re.search(r"Tests passed\s*:\s*1\b", tail)
            assert re.search(r"Tests skipped\s*:\s*0\b", tail) and "QA_PASS_PAYLOAD" not in tail
            if arm == "candidate":
                assert has_diff and any(re.search(r"-\s*QA_EXPECTED_PAYLOAD", s) for s in expected)
                assert any(re.search(r"\+\s*QA_OBSERVED_PAYLOAD", s) for s in observed)
            else:
                assert not has_diff and not expected and not observed
        else:
            assert run.returncode == 0 and re.search(r"PASS: \([^\n]+\) 1 tests passed", tail)
            assert "QA_PASS_PAYLOAD" not in tail and not has_diff
        result["cases"][name] = {
            "phpts": cases, "exit": run.returncode, "sha256": hashlib.sha256(raw).hexdigest(),
            "diff": has_diff, "expected": expected, "observed": observed,
        }
    (receipt / "result.json").write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
