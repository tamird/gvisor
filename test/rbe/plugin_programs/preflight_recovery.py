"""Recover the two original repository failures; query only if unavailable."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import traceback
from zipfile import ZipFile

from action_log import context, original_log
from cas_inputs import rpc
from qualify import OUT, TARGET, TMP, diagnostics, now, save

SOURCE = "835171376ef781d5efb818894a5ca6302fa8efc9"
ARCHIVE = "5bbcd45ae74196f2a949ba454fe32b5f81563f74e364eee1b7bbb0f620bb89c4"
QUERY = TARGET + ' + filter(":(pmd_config|pmdinfogen)$", deps(' + TARGET + '))'


def recover(r):
    data = (TMP / "original.zip").read_bytes()
    assert hashlib.sha256(data).hexdigest() == ARCHIVE
    with ZipFile(TMP / "original.zip") as z:
        original = json.loads(z.read("result.json"))
    assert original["source"] == SOURCE and original["control"]["source"] == SOURCE
    assert all(a["preflight"]["exit"] == 1 and not a.get("actions") for a in (original, original["control"]))
    r["original"] = original
    save(r)
    searches = [
        {"commitSha": SOURCE, "command": "cquery"},
        {"command": "cquery", "pattern": QUERY, "updatedAfter": "2026-11-02T09:13:30Z", "updatedBefore": "2026-11-02T09:15:30Z"},
    ]
    r["original_logs"] = []
    for query in searches:
        response = rpc("SearchInvocation", {"query": query, "count": 5})
        assert not response.get("nextPageToken")
        rows = response.get("invocation", [])
        if not rows:
            continue
        assert len(rows) == 2
        for row in rows:
            assert row.get("commitSha", "") in ("", SOURCE) and row["command"] == "cquery"
            item = {k: row.get(k) for k in ("invocationId", "commitSha", "host", "command", "pattern", "createdAtUsec", "updatedAtUsec", "bazelExitCode")}
            r["original_logs"].append(item)
            save(r)
            item["log"] = original_log(row["invocationId"])
            save(r)
        r["status"] = "RECOVERED_ORIGINAL_FAILURE"
        return True
    return False


def query(r):
    # The source is unchanged from the original positive arm. This fallback
    # only evaluates repositories and the target graph; it cannot build.
    consumer = Path("external/tools/plugin-stack/plugin-stack.BUILD").read_bytes()
    assert hashlib.sha256(consumer).hexdigest() == "f14ba6f6643ffc4afc3159f7a226f1c3cd0cb3183a13a1151ead118d3cd8cc92"
    platform = "//test/rbe/empty_worker:amd64_musl"
    flags = ["--config=x86_64", "--config=plugin-tldk", "-c", "opt",
             "--platforms=@llvm//platforms:linux_x86_64_musl", "--host_platform=" + platform,
             "--extra_execution_platforms=" + platform, "--remote_accept_cached", "--noremote_upload_local_results",
             "--noremote_local_fallback", "--spawn_strategy=remote", "--remote_download_outputs=toplevel",
             "--jobs=400", "--color=no", "--curses=no", "--registry=" + (Path(os.environ["GITHUB_WORKSPACE"]) / "registry").as_uri(),
             "--registry=https://bcr.bazel.build"]
    row = r["query_only"] = {"started": now(), "source": os.environ["GITHUB_SHA"]}
    save(r)
    env = {k: v for k, v in os.environ.items() if k != "BUILDBUDDY_API_KEY"}
    p = subprocess.run(["bazel", "--bazelrc=" + str(TMP / "sidecar.bazelrc"), "cquery", *flags,
                        "--output=label", QUERY], capture_output=True, text=True, timeout=420, env=env)
    row.update(exit=p.returncode, finished=now(), stderr_sha256=hashlib.sha256(p.stderr.encode()).hexdigest(), **diagnostics(p.stderr))
    save(r)
    if p.returncode:
        row["causal_context"] = context(p.stderr)
        r["status"] = "REPRODUCED_QUERY_FAILURE"
    else:
        row["selected"] = p.stdout.splitlines()
        r["status"] = "QUERY_PASSED_ORIGINAL_UNAVAILABLE"
    save(r)


if __name__ == "__main__":
    r = {"status": "INCOMPLETE", "original_run": 41418847041, "artifact": 13129061322,
         "archive_sha256": ARCHIVE, "source": SOURCE, "started": now()}
    save(r)
    recovered = False
    try:
        recovered = recover(r)
    except Exception as e:
        r["recovery_error"] = {"type": type(e).__name__, "line": traceback.extract_tb(e.__traceback__)[-1].lineno}
        save(r)
    if not recovered:
        try:
            query(r)
        except Exception as e:
            r["query_collection_error"] = {"type": type(e).__name__, "line": traceback.extract_tb(e.__traceback__)[-1].lineno}
    r["finished"] = now()
    save(r)
    raise SystemExit(0 if r["status"] != "INCOMPLETE" else 1)
