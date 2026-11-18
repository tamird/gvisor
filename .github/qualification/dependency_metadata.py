"""Record canonical workload image metadata without treating help exit1 as failure."""

import json
from pathlib import Path
import re
import subprocess
import sys

results = Path(sys.argv[1])
records = {}
for label, image, arguments in [
    ("redis-version", "redis", ["redis-benchmark", "--version"]),
    ("redis-help", "redis", ["redis-benchmark", "--help"]),
    ("httpd-inputs", "httpd", ["sh", "-c", "wc -c /local/latin*k.txt"]),
]:
    process = subprocess.run(["sudo", "docker", "run", "--rm", "--runtime=runc", f"gvisor.dev/images/benchmarks/{image}:latest", *arguments], text=True, capture_output=True, timeout=30)
    (results / (label + ".stdout.txt")).write_text(process.stdout)
    (results / (label + ".stderr.txt")).write_text(process.stderr)
    records[label] = {"exit": process.returncode, "argv": arguments}
    combined = process.stdout + process.stderr
    assert process.returncode in (0, 1), (label, process.returncode, combined)
    if label == "redis-version":
        assert re.search(r"redis-benchmark\s+\d+[.]\d+", combined), combined
    elif label == "redis-help":
        defaults = {}
        for option in ("d", "c"):
            match = re.search(r"-" + option + r"\s+<[^>]+>[^\n]*default\s*[:=]?\s*(\d+)", combined, re.IGNORECASE)
            assert match and int(match[1]) > 0, (option, combined)
            defaults[option] = int(match[1])
        records[label]["dataBytesPerValue"] = defaults["d"]
        records[label]["defaultClients"] = defaults["c"]
    else:
        assert process.returncode == 0, combined
        expected = {f"/local/latin{size}k.txt": size * 1024 for size in (1, 10, 100, 1024, 10240)}
        actual = {}
        for line in process.stdout.splitlines():
            amount, name = line.split()
            if name != "total":
                actual[name] = int(amount)
        assert actual == expected, actual
        records[label]["fileBytes"] = actual
(results / "dependency-metadata.json").write_text(json.dumps(records, indent=2) + "\n")
