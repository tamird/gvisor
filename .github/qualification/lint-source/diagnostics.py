"""Check only the revised CI diagnostics. No lint TestRunner is needed."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess

SOURCE = "869a7b324568a5743e32724096ed06207dcd0b93"
EXTRA = ("runsc/prewarmer/cpplint_untracked_control.cc", "test/util/cpplint_untracked_control.h")
CONFIG = ("CPPLINT.cfg", "test/util/CPPLINT.cfg")
FILES = "lint: untracked C/C++ files matched by CI lint tests:"
CONFIGS = "lint: CI lint tests do not support CPPLINT.cfg:"
work = Path(os.environ["RUNNER_TEMP"])
result = {"source": SOURCE, "phases": {}, "status": "INCOMPLETE"}


def verify(name, extra, config):
    inputs = extra + config
    calls = work / (name + ".calls")
    assert not calls.exists() and not any(Path(p).exists() for p in inputs)
    try:
        for path in extra:
            Path(path).write_text("// untracked fixture\n")
        for path in config:
            Path(path).write_text("filter=-build/include_order\n")
        p = subprocess.run(["tools/lint.sh", "cpplint"], env={**os.environ, "CPPLINT_CALLS": str(calls), "CI": "true"},
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        lines = p.stdout.decode().splitlines()
        row = {"exit": p.returncode, "calls": calls.read_text().splitlines(),
               "sources": (), "configs": (), "log_sha256": sha256(p.stdout).hexdigest()}
        result["phases"][name] = row
        for header, files, key in ((FILES, extra, "sources"), (CONFIGS, config, "configs")):
            assert lines.count(header) == int(bool(files))
            if files:
                index = lines.index(header) + 1
                row[key] = lines[index:index + len(files)]
                assert set(row[key]) == set(files)
        assert p.returncode != 0 and row["calls"] == ["query"]
    finally:
        for path in inputs:
            Path(path).unlink(missing_ok=True)

try:
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    verify("untracked", EXTRA, ())
    verify("config", (), CONFIG)
    verify("both", EXTRA, CONFIG)
    result["status"] = "PASS"
finally:
    (work / "cpplint-diagnostics.json").write_text(json.dumps(result, indent=2) + "\n")
