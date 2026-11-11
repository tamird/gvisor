"""Retain the installed versions, config fields and resource-update outcomes."""

import json
import os
from pathlib import Path
import re


tmp = Path(os.environ["RUNNER_TEMP"])
expected = {f"//test/root:crictl_test_{v.replace('.', '_')}_owned": v for v in ("1.7.31", "2.2.3")}
rows = {}
for manifest in (tmp / "qualification").glob("invocation-*/events.jsonl.manifest.json"):
    for report in json.loads(manifest.read_text())["reports"]:
        label = report.get("identity", {}).get("label")
        if label not in expected or report.get("name") != "test.log":
            continue
        log = (manifest.parent / report["path"]).read_text(errors="replace")
        version = expected[label]
        configs = {scope: field for scope, actual, field in re.findall(
            r"cri-pause config=(ordinary|sandbox_controller) version=([\d.]+) field=(\w+) image=gvisor.dev/images/cri/pause:latest",
            log,
        ) if actual == version}
        result = re.search(r"cri-pause version=" + re.escape(version) + r" resource_update=(true|false) registry_connects=(\d+)", log)
        outcome = re.search(r"--- (PASS|SKIP|FAIL): TestPauseImageConfig/resource_update(?: |$)", log)
        import_fingerprints = []
        for line in log.splitlines():
            if 'importing "/runtime/pause.tar"' in line:
                for fingerprint in ("not found", "no such file", "permission denied", "failed to unpack", "unsupported", "unknown"):
                    if fingerprint in line.lower():
                        import_fingerprints.append(fingerprint)
        rows[version] = {
            "config_fields": configs,
            "direct_keeps_default": f"cri-pause version={version} no_harness_preserves_default=true" in log,
            "resource_update": outcome[1] if outcome else "missing",
            "registry_connects": int(result[2]) if result else None,
            "import_failure_classes": sorted(set(import_fingerprints)),
            "observed_import_failure": 'importing "/runtime/pause.tar"' in log,
            "pull_failure": "failed to get sandbox image" in log or "failed to pull" in log,
            "pod_rpc_failure": "RunPodSandbox" in log and "rpc error" in log,
        }

(tmp / "cri-startup-results.json").write_text(json.dumps(rows, indent=2) + "\n")
print(json.dumps(rows))
for version in expected.values():
    row = rows.get(version, {})
    if (set(row.get("config_fields", {})) != {"ordinary", "sandbox_controller"}
            or not row.get("direct_keeps_default") or row.get("resource_update") != "PASS"
            or row.get("registry_connects") != 0):
        raise SystemExit(f"Missing or failed CRI validation for {version}")
