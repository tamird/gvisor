"""Summarize the original posture assertions in three digest-pinned test logs."""
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import exec_cas

ORIGINALS = {
    "ordinary-arm64": ("b31f8bfe-2484-48df-b737-5b9bc46052ac", "63f622be0f08d7d6a3bf09d661164eae7c65708f774b8d0ad662d727d421dc33", 216982),
    "mixed-arm64": ("466890c4-a777-4580-9c60-46796379a021", "da5d78f56d0323b7f2e992f1e21970916e89debdf22cc60d82d6c4015559088f", 222937),
    "mixed-amd64": ("466890c4-a777-4580-9c60-46796379a021", "4f0394b4f45145beeb346b1e15f9bd43bb0be73f0f9708e7e7c8488834073b86", 207463),
}
source = Path("pkg/test/sandboxposture/sandboxposture.go").read_text()
fields = set(re.findall(r"^\s*([A-Z][A-Za-z0-9]+)\s+(?:map\[|\[\]|[A-Za-z*])", source, re.M))
context_keys = set(re.findall(r'"([A-Z][A-Z0-9_]+)"', Path("pkg/test/sandboxposture/expected.go").read_text()))
words = (
    "operation not supported", "not supported", "permission denied", "operation not permitted",
    "no such file or directory", "invalid argument", "function not implemented", "no such process",
    "error collecting posture", "changed from", "not a directory", "no such device",
    "reading status", "reading reference status", "collecting credentials", "collecting capabilities",
    "collecting seccomp", "collecting namespaces", "collecting filesystem", "collecting fd table",
    "collecting process attributes", "collecting per-thread", "collecting core tags", "collecting children",
    "sandbox posture differs", "mismatched", "unexpected", "unknown", "unavailable",
)
enums = set(re.findall(r'"([a-z][a-z_-]{1,32})"', source))
report = {}
for name, (invocation, digest, size) in ORIGINALS.items():
    exec_cas.INV = invocation
    log = exec_cas.blob({"hash": digest, "sizeBytes": size}, size).decode(errors="replace")
    record = {"digest": digest, "subtests": {}, "assertions": []}
    current = None
    fields_seen = []
    active_case = None
    for line in log.splitlines():
        if "=== RUN" in line or "=== NAME" in line or "=== CONT" in line:
            case = re.search(r"=== (?:RUN|NAME|CONT)\s+TestBootPosture/(default|fdlimit|hostinet|no_directfs|no_directfs_hostinet)/(sentry|gofer)\b", line)
            active_case = "/".join(case.groups()) if case else None
        outcome = re.search(r"--- (PASS|FAIL|SKIP): TestBootPosture/(default|fdlimit|hostinet|no_directfs|no_directfs_hostinet)/(sentry|gofer) ", line)
        if outcome:
            record["subtests"]["/".join(outcome.groups()[1:])] = outcome[1]
        loc = re.search(r"\b(bootposture_test|sandboxposture|expected)\.go:(\d+):", line)
        if loc:
            current = {"case": active_case, "source": loc[1] + ".go:" + loc[2], "categories": [w for w in words if w in line.lower()], "changes": []}
            fields_seen = []
            record["assertions"].append(current)
            continue
        if current is None:
            continue
        if "Collected posture:" in line or line.lstrip().startswith(("===", "---")):
            current = None
            continue
        identified = [f for f in re.findall(r'\b([A-Z][A-Za-z0-9_]*)\b', line) if f in fields or f in context_keys]
        fields_seen = (fields_seen + identified)[-4:]
        if re.match(r"^\s*[-+]\s", line):
            change = {"side": line.lstrip()[0], "fields": list(fields_seen)}
            constants = [s for s in re.findall(r'"([a-z][a-z_-]{1,32})"', line) if s in enums]
            if constants:
                change["enums"] = constants[:8]
            if len(current["changes"]) < 16:
                current["changes"].append(change)
    record["assertions"] = record["assertions"][:40]
    report[name] = record
out = Path(os.environ["RUNNER_TEMP"]) / "posture-assertions.json"
out.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
