"""Collect only the named one-P scheduling experiment and its Bazel attempt."""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

temp = Path(os.environ["RUNNER_TEMP"])
out = temp / "owner"
events = temp / "owner.bep"
label = "//pkg/tcpip/transport/tcp:tcp_test"
log = Path("bazel-testlogs/pkg/tcpip/transport/tcp/tcp_test/test.log")
text = log.read_text() if log.exists() else ""
attempts = []
if events.exists():
    for line in events.read_text().splitlines():
        event = json.loads(line)
        if event["id"].get("testResult", {}).get("label") == label:
            row = event["testResult"]
            attempts.append({
                "status": row["status"],
                "cached": row.get("cachedLocally", False) or row.get("executionInfo", {}).get("cachedRemotely", False),
                "strategy": row.get("executionInfo", {}).get("strategy"),
                "started": row.get("testAttemptStart"),
                "duration": row.get("testAttemptDuration"),
            })

pattern = re.compile(r"OWNER_SAMPLE pair=(\d+) yield=(true|false) ns=(\d+) retries=(\d+) yields=(\d+) peer_at_resume=(true|false) peer_at_release=(true|false) peer_served=(true|false) held_served=(true|false) capped=(true|false)")
samples = [
    dict(pair=int(pair), policy="yield" if y == "true" else "retry", ns=int(ns),
         retries=int(retries), yields=int(yields), peer_at_resume=resume == "true", peer_at_release=peer == "true",
         peer_served=served == "true", held_served=held == "true", capped=capped == "true")
    for pair, y, ns, retries, yields, resume, peer, served, held, capped in pattern.findall(text)
]
runtime = re.findall(r"OWNER_ENV go=(\S+) os=(\S+) arch=(\S+) procs=(\d+) godebug=(\S+)", text)
expected = [(i, policy) for i in range(12) for policy in (("retry", "yield") if i % 2 == 0 else ("yield", "retry"))]
gates = {
    "exit": (out / "bazel-exit.txt").read_text().strip() == "0" if (out / "bazel-exit.txt").exists() else False,
    "fresh_pass": len(attempts) == 1 and attempts[0]["status"] == "PASSED" and not attempts[0]["cached"],
    "named_pass": len(re.findall(r"--- PASS: TestSinglePRunnableOwner \(", text)) == 1,
    "linux_one_p": runtime == [("go1.27.3", "linux", "amd64", "1", "asyncpreemptoff=0")],
    "all_pairs": [(r["pair"], r["policy"]) for r in samples] == expected,
    "both_served": all(r["peer_served"] and r["held_served"] for r in samples),
    "uncapped": all(not r["capped"] for r in samples),
    "policy": all(r["ns"] > 0 and r["retries"] > 0 and r["yields"] == (r["retries"] if r["policy"] == "yield" else 0) for r in samples),
}
measured = all(gates.values())
result = dict(source=os.environ["GITHUB_SHA"], attempts=attempts, runtime=runtime, gates=gates,
              samples=samples, verdict="MEASURED" if measured else "INCOMPLETE", observed=datetime.now(timezone.utc).isoformat())
(out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
for policy in ("retry", "yield"):
    (out / f"{policy}.bench").write_text("".join(
        f"BenchmarkOwnerResume 1 {r['ns']} ns/op {r['retries']} retries/op\n"
        for r in samples if r["policy"] == policy and not r["capped"]
    ))
print(result["verdict"], gates)
raise SystemExit(0 if measured else 1)
