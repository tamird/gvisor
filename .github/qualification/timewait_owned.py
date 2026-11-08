"""Check active TIME_WAIT reuse and its original connection race on one native owner."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import time
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

import public_assets as assets
import tldk_public_inputs as inputs

WORK = Path(os.environ["RUNNER_TEMP"]) / "timewait"
OUT = WORK / "results"
ENDPOINT = "pkg/tcpip/transport/tcp/endpoint.go"
TEST = "pkg/tcpip/transport/tcp/test/e2e/tcp_test.go"
OWNER = "//pkg/tcpip/transport/tcp/test/e2e:tcp_test"
MASTER = "451b92800b9cb82262dc8a735388f7cf4d17d895"
FIX = "64ba857aaadc2f18781a1b7f998fe5a4836bc6e4"
NEG = "TestTimeWaitReuseWithoutTimestamps"
POS = "TestTimeWaitAssassination"
LEAVES = {f"{POS}/{p}" for p in range(1024, 1049)}
LIMIT = time.monotonic() + 65*60
dirs = {assets.DIST}
seen = {}
report = {"master": MASTER, "source": FIX, "owner": OWNER, "kernel": platform.release(),
          "userspace": platform.machine(), "linux_reference": 13107777937,
          "linux_zip_sha256": "f5ec27cd11064e8283d0c184bda938131203750be713210fcf50a770796d6fb4",
          "arms": {}, "public_distfiles": seen}


def save():
    (OUT / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args])


def recover(text, known):
    urls = set(re.findall(r"Error downloading \[(https://github[.]com/[^\]\s,]+)\] to ", text))
    progress = False
    for url in sorted(urls - seen.keys()):
        if len(seen) >= 48:
            break
        budget = (1<<30) - sum(x.get("bytes", 0) for x in seen.values())
        try:
            if url in known:
                directory, item = inputs.materialize(*known[url], budget)
            elif assets.URL.fullmatch(url):
                directory, item = assets.release_distfile(url, assets.DIST, remaining_bytes=budget)
            else:
                continue
            seen[url] = item
            progress |= directory not in dirs
            dirs.add(directory)
        except (OSError, ValueError, KeyError, AssertionError) as exc:
            seen[url] = {"unavailable": type(exc).__name__ + " " + str(exc)[:180]}
    save()
    return progress


def observe(arm, row, event):
    result = event["testResult"]
    info = result.get("executionInfo", {})
    row.update(status=result["status"], strategy=info.get("strategy"),
               cached=bool(result.get("cachedLocally") or info.get("cachedRemotely")))
    files = row["files"] = {}
    for item in result["testActionOutput"]:
        name = item["name"]
        if name not in ("test.log","test.xml"):
            continue
        uri = urlsplit(item["uri"])
        assert uri.scheme == "file" and not uri.netloc
        data = Path(unquote(uri.path)).read_bytes()
        h = hashlib.sha256(data).hexdigest()
        assert 0 < len(data) <= 4<<20 and (not item.get("digest") or item["digest"] == h)
        assert "length" not in item or int(item["length"]) == len(data)
        (OUT / (arm+"."+name.split(".")[1])).write_bytes(data)
        files[name] = {"bytes": len(data), "sha256": h}
    log = (OUT/(arm+".log")).read_text()
    xml = ET.parse(OUT/(arm+".xml"))
    cases = re.findall(r"--- (PASS|FAIL|SKIP): ([^\s]+) [(]", log)
    counts = Counter(name for _, name in cases)
    row.update(cases=cases, xml_names=sorted(t.get("name","") for t in xml.iter("testcase")))
    assert not row["cached"] and row["strategy"] in ("local","standalone")
    assert counts and all(n == 1 for n in counts.values())
    if arm == "stock":
        row["diagnostic"] = [l.strip() for l in log.splitlines() if "Connect = " in l]
        assert row["bazel_exit"] == 3 and result["status"] == "FAILED" and cases == [("FAIL",NEG)]
        assert "Connect = connection attempt started, want ErrNoPortAvailable" in log
    else:
        assert row["bazel_exit"] == 0 and result["status"] == "PASSED"
        assert set(counts) == LEAVES | {POS,NEG} and all(s == "PASS" for s,_ in cases)
        assert not re.search(r"WARNING: DATA RACE|race detected during execution", log)
    assert (LEAVES | {NEG} if arm != "stock" else {NEG}) <= set(row["xml_names"])


def run(arm, known):
    endpoint = MASTER if arm == "stock" else FIX
    Path(ENDPOINT).write_bytes(git("show",endpoint+":"+ENDPOINT))
    row = report["arms"][arm] = {"endpoint":git("hash-object",ENDPOINT).decode().strip(),
                                 "fixture":git("hash-object",TEST).decode().strip(),"preparation":[]}
    assert row["fixture"] == "d9a1388d67789be0afb14bcad504b22786c9168b"
    assert row["endpoint"] == ("56afdaeb42935493339fd1700f5c12ae04e708e6" if arm=="stock" else "4fd57dd821e1e5391b6421b36164dd279fc2c988")
    bep, stdout = WORK/(arm+".bep"), WORK/(arm+".bazel.log")
    for _ in range(40):
        bep.unlink(missing_ok=True)
        argv = [str(WORK/"bazel"),"--nosystem_rc","--nohome_rc","--host_jvm_args=-Xmx4g","test",
                "--config=x86_64",*(["--config=race"] if arm=="race" else []),"--jobs=4",
                "--remote_executor=","--remote_cache=","--bes_backend=","--lockfile_mode=off",
                *("--distdir="+str(d) for d in sorted(dirs)),"--strategy=TestRunner=local",
                "--nocache_test_results","--runs_per_test=1","--flaky_test_attempts=1","--test_timeout=360",
                "--test_sharding_strategy=disabled","--test_env=GO_TEST_WRAP_TESTV=1",
                "--build_event_json_file="+str(bep),"--build_event_json_file_path_conversion=false",
                "--test_filter=^("+ (NEG if arm=="stock" else NEG+"|"+POS) +")$", OWNER]
        save()
        with stdout.open("wb") as out:
            if time.monotonic() >= LIMIT:
                raise TimeoutError("qualification limit before another build")
            row["bazel_exit"] = subprocess.run(argv,stdout=out,stderr=subprocess.STDOUT,timeout=LIMIT-time.monotonic()).returncode
        events = [json.loads(l) for l in bep.open()] if bep.exists() else []
        results = [e for e in events if "testResult" in e]
        row["invocation"] = next((e["started"]["uuid"] for e in events if "started" in e),None)
        if results:
            e, = results
            assert e["id"]["testResult"]["label"] == OWNER
            observe(arm,row,e)
            row["verified"] = True
            return
        log = stdout.read_text(errors="replace")
        row["preparation"].append({"exit":row["bazel_exit"],"errors":[s[:500] for s in log.splitlines() if s.startswith("ERROR:")][:6]})
        if not recover(log,known):
            raise RuntimeError("no TestResult; see original preparation diagnostics")
    raise RuntimeError("preparation limit")


def main():
    assert platform.machine() == "x86_64" and git("rev-parse","HEAD").decode().strip() == FIX
    manifest = json.loads((WORK/"inputs.json").read_bytes())
    known = {v[2]:v for v in manifest}
    coral = "https://github.com/google-coral/crosstool/archive/8e885509123395299bed6a5f9529fdc1b9751599.tar.gz"
    sha = "f86d488ca353c5ee99187579fe408adb73e9f2bb1d69c6e3a42ffb904ce3ba01"
    rule = Path("tools/bazeldefs/extensions/coral_crosstool.bzl").read_text()
    assert coral in rule and ('sha256 = "'+sha+'"') in rule
    known[coral] = [None,None,coral,sha,17312,None]
    selected = {"platforms","bazel_skylib","rules_go","rules_android","rules_cc","gazelle","rules_python","bazel_lib","rules_shell","rules_pkg","protobuf"}
    assets.DIST.mkdir(parents=True,exist_ok=True)
    for directory,entry in inputs.seed([v for v in manifest if v[0] in selected] + [known[coral]]):
        if directory:
            seen[entry["source"]] = entry
            dirs.add(directory)
    for arm in ("stock","normal","race"):
        try:
            run(arm,known)
        except Exception as e:
            report["arms"].setdefault(arm,{})["error"] = type(e).__name__+" "+str(e)[:360]
        finally:
            save()
    assert all(a.get("verified") for a in report["arms"].values())


try:
    main()
    report["verified"] = True
finally:
    Path(ENDPOINT).write_bytes(git("show",FIX+":"+ENDPOINT))
    save()
