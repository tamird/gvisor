"""Run both native test owners and check each focused mutation's observed failure."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

WORK = Path(os.environ["RUNNER_TEMP"]) / "checkpoint-format-tests"
FORWARD = "//runsc/cmd/sentry/forwardcmd:forwardcmd_test"
STATE = "//pkg/state:state_test"
GRAPH = "TestPrintGraphSliceNotation"
DUMP = "TestStatefileDumpMemoryFiles"
LEAVES = ("Split", "Legacy", "ZeroOwners", "Inline", "GenericCollision", "WrongKernelRoot",
          "MissingOwners", "NonSliceOwners", "MissingPrivate", "ExtraPrivate", "ShortMetadata", "ShortPage")
SOURCE = "ebcea451de61a61957c82527fa94241827267cde"
FLAGS = ["--config=aarch64", "-c", "opt", "--strip=never", "--jobs=3", "--local_resources=memory=9900",
         "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
         "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
         "--remote_executor=", "--remote_cache=", "--bes_backend=", "--spawn_strategy=linux-sandbox",
         "--strategy=TestRunner=standalone", "--nocache_test_results", "--flaky_test_attempts=1",
         "--test_sharding_strategy=disabled", "--runs_per_test=1", "--test_env=GO_TEST_WRAP_TESTV=1",
         "--test_timeout=900", "--noshow_progress", "--color=no", "--curses=no"]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def evidence(label, result):
    xml = log = None
    hashes = {}
    for item in result["testActionOutput"]:
        name = item["name"]
        if name not in ("test.log", "test.xml"):
            continue
        url = urlsplit(item["uri"])
        assert url.scheme == "file" and not url.netloc
        path = Path(unquote(url.path)).resolve(strict=True)
        expected = Path("bazel-testlogs") / label[2:].replace(":", "/") / name
        assert path == expected.resolve(strict=True) and path.stat().st_size < 12 << 20 and name not in hashes
        raw = path.read_bytes()
        hashes[name] = sha(raw)
        assert not item.get("digest") or item["digest"] == hashes[name]
        if "length" in item:
            assert int(item["length"]) == len(raw)
        if name == "test.log":
            log = raw.decode(errors="replace")
        else:
            xml = ET.fromstring(raw)
    assert xml is not None and log is not None
    return log, xml, hashes


def test(name, targets, row, failed=None, witness=None):
    bep = WORK / (name + ".bep")
    args = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3000m",
            "test", *FLAGS, "--build_event_json_file="+str(bep)]
    if failed:
        selector = "/".join("^"+re.escape(piece)+"$" for piece in failed.split("/"))
        args += ["--test_filter="+selector, "--test_arg=-test.run="+selector]
    with (WORK / (name+".log")).open("wb") as output:
        proc = subprocess.run(args+list(targets), stdout=output, stderr=subprocess.STDOUT, timeout=4200)
    row.update(exit=proc.returncode, results={})
    for line in bep.read_text().splitlines():
        event = json.loads(line)
        if "finished" in event:
            row["bazel_exit"] = event["finished"].get("exitCode", {}).get("code", 0)
        if "testResult" not in event:
            continue
        label = event["id"]["testResult"]["label"]
        assert label in targets and label not in row["results"]
        result = event["testResult"]
        entry = row["results"][label] = {"status":result["status"],"cached":bool(result.get("cachedLocally")),
               "remote_cached":bool(result.get("executionInfo",{}).get("cachedRemotely")),
               "strategy":result.get("executionInfo",{}).get("strategy")}
        log, xml, entry["digests"] = evidence(label, result)
        starts = re.findall(r"(?m)^\x16?\s*=== RUN\s+(\S+)\s*$",log)
        ends = re.findall(r"(?m)^\x16?\s*--- (PASS|FAIL|SKIP):\s+(\S+) \(",log)
        cases = targets[label]
        entry["named"] = {n: [res for res,case in ends if case==n] for n in sorted(cases)}
        entry["xml_failures"] = len(xml.findall(".//failure")) + len(xml.findall(".//error"))
        entry["message_seen"] = witness in log if witness else None
        assert all(starts.count(n)==1 and entry["named"][n]==["FAIL" if failed else "PASS"] for n in cases)
        assert entry["strategy"] in ("local","standalone") and not entry["cached"] and not entry["remote_cached"]
        if failed:
            assert set(c for res,c in ends if res=="FAIL") <= {failed,failed.split("/")[0]}
            assert entry["status"]=="FAILED" and entry["xml_failures"] and entry["message_seen"]
        else:
            assert entry["status"]=="PASSED" and not entry["xml_failures"]
    assert row["results"].keys()==targets.keys() and row["bazel_exit"]==row["exit"]==(3 if failed else 0)
    row["verified"] = True


def main():
    receipt = {"product": SOURCE, "cpu": platform.machine(), "uid": os.getuid(), "runs": {}, "verified":False}
    assert receipt["cpu"] == "aarch64" and receipt["uid"] != 0
    def arm(name):
        row = {"verified": False}
        receipt["runs"][name] = row
        return row

    try:
        def observe(name, targets, row, *expected):
            try:
                test(name, targets, row, *expected)
            except (AssertionError, OSError, ValueError, KeyError) as error:
                row["observer_error"] = type(error).__name__
        observe("owners", {STATE:{GRAPH},FORWARD:{DUMP+"/"+n for n in LEAVES}}, arm("owners"))
        for key,path,before,after,owner,case,reason in [
            ("graph-capacity","pkg/state/graph_print_unsafe.go","int64(f.Len())","int64(f.Cap())",
             STATE,GRAPH,'root = ("state.printSliceHolder", 8), want ("state.printSliceHolder", 2)'),
            ("short-page","pkg/sentry/pgalloc/metadata/metadata.go","io.CopyN(io.Discard, r, int64(size))",
             "io.Copy(io.Discard, io.LimitReader(r, int64(size)))",FORWARD,DUMP+"/ShortPage",
             "dumpCheckpoint error = <nil>, want rejection"),
            ("raw-region-size","pkg/state/pretty/pretty.go",
             'fmt.Fprintf(w, "(%d bytes non-object data)\\n", n)', 'fmt.Fprintf(w, "(%d bytes non-object data)\\n", 1)',
             FORWARD,DUMP+"/Inline","missing application or private page region"),
            ("html-as-text","runsc/cmd/sentry/forwardcmd/statefile.go","dumpCheckpoint(output, rc, s.html)",
             "dumpCheckpoint(output, rc, false)",FORWARD,DUMP+"/Inline","html=true: unexpected graph anchor")]:
            p = Path(path)
            original = p.read_text()
            assert original.count(before)==1
            pristine = subprocess.check_output(["git","show",SOURCE+":"+path])
            assert original.encode()==pristine
            row = arm(key)
            try:
                p.write_text(original.replace(before, after))
                row.update(path=path, original=sha(pristine), mutation=sha(p.read_bytes()))
                observe(key, {owner:{case}}, row, case, reason)
            finally:
                p.write_bytes(pristine)
        receipt["verified"] = all(v["verified"] for v in receipt["runs"].values())
        assert receipt["verified"]
    finally:
        (WORK/"result.json").write_text(json.dumps(receipt,indent=2)+"\n")


if __name__ == "__main__":
    main()
