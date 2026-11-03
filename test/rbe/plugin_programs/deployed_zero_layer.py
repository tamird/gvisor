"""Run the retained zero-layer action against the hosted OCI executor."""
import json
import os
from pathlib import Path
import subprocess

from qualify import now, TMP
from scratch_root import PLATFORM, run_probe

ORIGINAL = "337f2c057ceb34cb842e552dec47c73ea3cbe82e"
DIGEST = {"hash": "03f60dcce40410708095a3d8ba0a54d1095bde32c1466e4a3dedc2df21e72a9a", "sizeBytes": "365"}
MANIFEST = "sha256:3d455195f1141b05cb8cc6deb07ea96b617e5436c7a259d9f3e7b1985f4d3f51"
CONFIG = "sha256:132144ab4e026a9b1ca95b5dfeb9892d12f0c2c1368ae0b4bdca408e62768396"
OWNER = "//test/rbe/empty_worker:no_layers"
receipt = {"status": "INCOMPLETE", "phase": "original action", "carrier": os.environ["GITHUB_SHA"],
           "fixture": ORIGINAL, "original_artifact": 13133351604, "target": OWNER, "started": now()}


def save():
    (TMP / "zero-layer.json").write_text(json.dumps(receipt, indent=2) + "\n")


def main():
    save()
    paths = subprocess.check_output(["git", "diff", "--name-only", ORIGINAL, "HEAD"], text=True).splitlines()
    assert set(paths) == {".github/workflows/build.yml", "test/rbe/plugin_programs/deployed_zero_layer.py"}
    flags = ["--config=x86_64", "--config=plugin-tldk", "-c", "opt", "--platforms=@llvm//platforms:linux_x86_64_musl",
             "--host_platform=" + PLATFORM, "--extra_execution_platforms=" + PLATFORM, "--noremote_upload_local_results",
             "--noremote_local_fallback", "--spawn_strategy=remote", "--remote_download_outputs=toplevel", "--jobs=1",
             "--color=no", "--curses=no", "--registry=" + (Path(os.environ["GITHUB_WORKSPACE"]) / "registry").as_uri(),
             "--registry=https://bcr.bazel.build"]
    data = {"probes": {}}
    try:
        passed = run_probe(data, "no_layers", ["bazel", "--bazelrc=" + str(TMP / "sidecar.bazelrc")], flags)
    finally:
        row = data["probes"].get("no_layers", {})
        for key in ("invocation", "exit", "finished", "diagnostics", "report_sha256", "stderr_sha256", "lookup_error"):
            if key in row:
                receipt[key] = row[key]
        if action := row.get("action"):
            receipt.update(observed_digest=action.get("digest"), runner=action.get("runner"),
                           cache_hit=bool(action.get("cacheHit")))
        elif row.get("attempts"):
            receipt["attempts"] = [{k: a.get(k) for k in ("digest", "stage", "codes", "diagnostics")}
                                   for a in row["attempts"]]
        if inventory := row.get("inventory"):
            assert inventory["platform_manifest"] == MANIFEST and inventory["config_digest"] == CONFIG
            assert inventory["layers"] == [] and inventory["rootfs"].get("diff_ids", []) == [] and inventory["files"] == {}
            receipt.update(manifest=MANIFEST, config=CONFIG, layers=0, diff_ids=0)
        save()
    if passed:
        a = row["action"]
        assert row["verified"] and a["runner"] == "remote" and not a.get("cacheHit") and a["digest"] == DIGEST
        receipt.update(status="PASS", phase="action completed", action=DIGEST, runner=a["runner"], cache_hit=False)
    elif row.get("bound_instantiation_failure"):
        assert any(a.get("digest") == DIGEST and a.get("stage") in (4, "COMPLETED") and
                   any("overlayfs" in d or "lowerdir" in d for d in a.get("diagnostics", []))
                   for a in row["attempts"])
        receipt.update(status="FAIL", phase="executor still fails to instantiate zero-layer image", action=DIGEST)
    else:
        receipt["phase"] = "no verified zero-layer execution or matching mount failure"
        # Do not infer capacity from UNAVAILABLE: the original mount failure used the same code.
        from cas_inputs import rpc
        try:
            rows = rpc("GetExecution", {"executionLookup": {"invocationId": row["invocation"]},
                                        "inlineExecuteResponse": True}).get("execution", [])
            selected = [e for e in rows if e.get("actionDigest") == DIGEST
                        and e.get("targetLabel", "").lstrip("@") == OWNER and e.get("actionMnemonic") == "ObserveWorkerRoot"]
            for e in selected:
                statuses = [e.get("status", {}), e.get("executeResponse", {}).get("status", {})]
                receipt["codes"] = [x.get("code", 0) for x in statuses]
                if any(phrase in x.get("message", "").lower() for x in statuses for phrase in
                       ("no registered executor", "no executors available", "no eligible executor", "no registered host",
                        "no available executor", "waiting for executor", "scheduler is unavailable")):
                    receipt["phase"] = "executor scheduling or capacity"
        except Exception as err:
            receipt["lookup_error"] = type(err).__name__
    receipt["verdict_at"] = now()
    save()
    return 0 if passed else row.get("exit") or 1


if __name__ == "__main__":
    raise SystemExit(main())
