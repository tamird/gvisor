"""Retain the completed native controls and qualify the final Nogo filter inputs."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

TMP = Path(os.environ["RUNNER_TEMP"]) / "runfiles"
NOGO = "//test/cmd/test_app:test_app_nogo"
ARCHES = {"amd64", "arm64"}
COMMON = ["--jobs=4", "--remote_cache=grpcs://remote.buildbuddy.io", "--remote_upload_local_results=false"]
REPORT = {"status": "INCOMPLETE", "carrier": os.environ["GITHUB_SHA"]}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def save():
    (TMP / "result.json").write_text(json.dumps(REPORT, indent=2, sort_keys=True) + "\n")


def bazel(section, verb, *args):
    log = TMP / (section + ".stderr")
    with log.open("wb") as stderr:
        flags = [] if verb == "query" else COMMON
        p = subprocess.run(["bazel", verb, *flags, *args], stdout=subprocess.PIPE, stderr=stderr)
    row = REPORT[section]
    row.update(exit=p.returncode, finished=now(), stderr_sha256=sha256(log.read_bytes()).hexdigest())
    if p.returncode:
        for line in log.read_text(errors="replace").splitlines():
            if line.startswith("ERROR:") and len(line) < 600:
                print(line, flush=True)
    save()
    require(p.returncode == 0, verb + " failed")
    return p.stdout


def original():
    data = TMP / "original.zip"
    digest = sha256(data.read_bytes()).hexdigest()
    require(digest == "d122d865cd07f90277f4fb1bdbe8bc56faa90fbf115240d40f84b5cf64abd9bc", "original ZIP digest")
    with zipfile.ZipFile(data) as z:
        require(z.namelist() == ["result.json"], "original members")
        prior = json.loads(z.read("result.json"))
    require(prior["carrier"] == os.environ["ORIGINAL"] and prior["status"] == "INCOMPLETE", "original identity")
    stock, fixed = (prior["arms"][s] for s in ("stock", "fixed"))
    require(stock["source"] == os.environ["COMPILER"] and fixed["source"] == os.environ["PRIOR"]
            and stock["observed"] and fixed["error"] == "missing transitioned original binary analysis actions", "original stopping point")
    require(stock["go_copy"]["copy_errno"] == 21 and stock["cpp"]["execve_errno"] == 13 and stock["cpp"]["exit"] == 1
            and fixed["go_copy"]["copy_errno"] == 0 and fixed["go_copy"]["exit"] == fixed["cpp"]["exit"] == 0
            and fixed["cpp"]["failures"] == 0 and fixed["static_amd64"], "original native outcomes")
    REPORT["original"] = {"artifact": 13263795278, "zip_sha256": digest, **prior}


def switch(ref):
    subprocess.run(["git", "switch", "--quiet", "--detach", ref], check=True)
    require(subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == ref, "wrong current source")


def expansion(section):
    # This package's actual expanded declarations, including with_cfg's private binary.
    data = bazel(section, "query", "//test/cmd/test_app:*", "--output=build", "--order_output=full")
    REPORT[section].update(bytes=len(data), sha256=sha256(data).hexdigest())
    require(b'test_app_' in data and b'basename = "test_app"' in data, "missing expanded target")


def graph(section):
    # Inspect the inputs actually consumed by the Nogo test's two final filters.
    # This query targets the test rule itself and does not select transitive aspects.
    raw = json.loads(bazel(section, "aquery", 'mnemonic("GoStaticAnalysis", ' + NOGO + ')',
                           "--output=jsonproto", "--noinclude_artifacts", "--include_commandline"))
    rows = REPORT[section]["filters"] = {}
    for action in raw.get("actions", []):
        args = action.get("arguments", [])
        outs = [a for a in args if a.startswith("-output=")]
        require(len(outs) == 1 and "filter" in args, "unexpected owner action")
        m = re.search(r"/test/cmd/test_app/test_app_nogo[.](amd64|arm64)[.]findings$", outs[0])
        require(m and m[1] not in rows, "missing or duplicate arch")
        inputs = args[args.index(outs[0]) + 1:]
        rows[m[1]] = {"input_count": len(inputs), "binary_input_paths": [a for a in inputs if "/test/cmd/test_app/" in a],
                      "owner_config_id": action.get("configurationId")}
    require(set(rows) == ARCHES, "owner did not create both final filters")
    save()
    suffix = "/test/cmd/test_app/" + ("test_app_/test_app" if section == "old_graph" else "test_app__/test_app_") + ".findings"
    require(all(len(v["binary_input_paths"]) == 1 and v["binary_input_paths"][0].endswith(suffix) for v in rows.values()),
            "final filter lacks expected binary-owned findings")
    require(len({v["binary_input_paths"][0] for v in rows.values()}) == 2, "architecture splits share binary findings")


def check():
    bep = TMP / "nogo.bep"
    try:
        bazel("owner", "test", "--nocache_test_results", "--build_event_json_file=" + str(bep), NOGO)
    finally:
        row = REPORT["owner"]
        if bep.exists():
            attempts = [e["testResult"] for line in bep.read_text().splitlines() if
                        (e := json.loads(line)).get("id", {}).get("testResult", {}).get("label") == NOGO]
            row["attempts"] = [{"status": a["status"], "cached": bool(a.get("cachedLocally") or a.get("cachedRemotely") or
                                                                    a.get("executionInfo", {}).get("cachedRemotely"))} for a in attempts]
        log = Path("bazel-testlogs/test/cmd/test_app/test_app_nogo/test.log")
        if log.exists():
            row["log_sha256"] = sha256(log.read_bytes()).hexdigest()
            row["sections"] = re.findall(r"^(amd64|arm64)\.\.\.PASS$", log.read_text(), re.M)
        save()
    require(row["attempts"] == [{"status": "PASSED", "cached": False}] and sorted(row["sections"]) == sorted(ARCHES), "missing fresh two-arch check")


original()
save()
for key, source, fn in [("old_graph", "COMPILER", graph), ("prior_expansion", "PRIOR", expansion),
                        ("final_expansion", "FIXED", expansion), ("final_graph", "FIXED", graph), ("owner", "FIXED", None)]:
    ref = os.environ[source]
    REPORT[key] = {"source": ref}
    try:
        switch(ref)
        fn(key) if fn else check()
        REPORT[key]["verified"] = True
    except Exception as exc:
        REPORT[key]["error"] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
    save()
REPORT["expansions_equal"] = REPORT["prior_expansion"].get("sha256") is not None and REPORT["prior_expansion"].get("sha256") == REPORT["final_expansion"].get("sha256")
REPORT["finished"] = now()
if REPORT["expansions_equal"] and all(REPORT[k].get("verified") for k in ("old_graph", "prior_expansion", "final_expansion", "final_graph", "owner")):
    REPORT["status"] = "VERIFIED"
save()
raise SystemExit(REPORT["status"] != "VERIFIED")
