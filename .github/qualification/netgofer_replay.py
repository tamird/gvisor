"""Five separate native runs of the original netgofer owner."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
import sys
from urllib import error, parse, request

WORK = Path(os.environ["RUNNER_TEMP"]) / "netgofer-replay"
TARGET = "//runsc/gofer/net:netgofer_test"
SOURCE = "4eb27c4081e535d69c1257998a6c94256d9812e2"
INV = "e4a4dd1a-49f3-48dc-9f88-b73472a088fd"
ORIGINAL = "870d20abeefdc0e6e4953cce4a1b83c3d46053b77dc61c28359ab0374ab516f7"
IMAGE = "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"
CASES = {"TestSpliceAndCloseWrite", "TestRunProxyModeEndToEnd", "TestRunProxyModeWithSeccomp",
         "TestProxyLimitsConnections", "TestProxyRejectsDisallowedDestinations",
         "TestNetgoferEarlyValidationClosesUDSFD"}
CASES.update("TestSpliceAndCloseWrite/" + sub for sub in (
    "half_close_and_bidirectional_copy", "aborts_on_transport_error", "close_write_fallback"))
CASES.update("TestProxyRejectsDisallowedDestinations/" + sub for sub in (
    "ipv4_current_network_subnet", "ipv4_link_local_unicast", "ipv4_local_interface_ip",
    "ipv6_link_local_unicast"))


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def fetch(req, limit, key):
    req.add_header("x-buildbuddy-api-key", key)
    try:
        with request.build_opener(NoRedirect).open(req, timeout=30) as response:
            raw = response.read(limit + 1)
    except error.HTTPError as exc:
        text = exc.read(400).lower()
        if b"permissiondenied" in text or b"unauthenticated" in text:
            raise RuntimeError("permission_denied") from None
        if b"notfound" in text:
            raise RuntimeError("not_found") from None
        raise RuntimeError(f"http_{exc.code}") from None
    if len(raw) > limit:
        raise RuntimeError("size_bound")
    return raw


def stack_frames(lines):
    project = ("gvisor.dev/gvisor/runsc/gofer/net", "gvisor.dev/gvisor/pkg/tcpip",
               "gvisor.dev/gvisor/pkg/sync", "gvisor.dev/gvisor/pkg/rawfile",
               "gvisor.dev/gvisor/pkg/sleep", "golang.org/x/sys/unix")
    std = ("runtime", "testing", "syscall", "internal/poll", "internal/sync", "sync",
           "os", "os/exec", "net", "io", "time", "context")
    stack = []
    stacks = []
    for line, following in zip(lines, lines[1:]):
        if re.match(r"^goroutine \d+\b", line):
            if stack and stack not in stacks:
                stacks.append(stack)
            stack = []
        fun = re.fullmatch(r"(?:created by )?([A-Za-z0-9_./-]+\.(?:\(\*?[A-Za-z0-9_]+\)\.)?[A-Za-z_][A-Za-z0-9_.]{0,96})(?:\(.*\)| in goroutine [0-9]+)", line)
        loc = re.fullmatch(r"\s*(?:\S*/)?([A-Za-z0-9_]+\.(?:go|s)):(\d{1,7})(?:\s+\+0x[0-9a-f]+)?\s*", following)
        if not fun or not loc:
            continue
        name = fun[1]
        if not (any(name.startswith(p + ".") or name.startswith(p + "/") for p in project)
                or any(name.startswith(p + ".") for p in std)):
            continue
        stack.append([name, f"{loc[1]}:{loc[2]}"])
    if stack and stack not in stacks:
        stacks.append(stack)
    stacks.sort(key=lambda s: not any("runsc/gofer/net" in f for f, _ in s))
    return [s[:20] for s in stacks[:8]]


def original():
    key = os.environ.pop("BUILDBUDDY_API_KEY")
    query = parse.urlencode({"invocation_id": INV, "bytestream_url": f"bytestream://remote.buildbuddy.io/blobs/{ORIGINAL}/1274"})
    data = fetch(request.Request("https://remote.buildbuddy.io/file/download?" + query), 1274, key)
    assert len(data) == 1274 and hashlib.sha256(data).hexdigest() == ORIGINAL and key.encode() not in data
    (WORK / "original.log").write_bytes(data)


def run():
    # The test-only shell reports the same native ELF Bazel will execute.
    shell = ('echo NATIVE_WORKER=$(/bin/uname -m); '
             'echo NATIVE_MACHINE=$(/usr/bin/od -An -j18 -N2 -tu2 "$1"); '
             'h=$(/usr/bin/sha256sum "$1"); echo NATIVE_TEST_SHA256=${h%% *}; exec "$@"')
    owner_file = WORK / "one-owner"
    owner_file.write_text(TARGET + "\n")
    for attempt in range(1, 6):
        dest = WORK / str(attempt)
        dest.mkdir()
        command = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--bazelrc=" + str(WORK / "private.bazelrc"),
                   "test", "--config=source-presubmit", "--target_pattern_file=" + str(owner_file),
                   "--nocache_test_results", "--flaky_test_attempts=1", "--modify_execution_info=^TestRunner$=+no-remote-cache",
                   "--test_timeout=60", "--test_arg=-test.timeout=40s", "--test_arg=-test.v", "--test_env=GO_TEST_WRAP_TESTV=1",
                   "--run_under=" + shlex.join(["/bin/sh","-c",shell,"native-cpu"]),
                   "--build_event_json_file=" + str(dest / "bep"), "--execution_log_compact_file=" + str(dest / "compact")]
        start = time.monotonic()
        with (dest / "console").open("wb") as stream:
            p = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT)
        (dest / "timing.json").write_text(json.dumps({"exit":p.returncode, "bazel_ms":round((time.monotonic()-start)*1000)}))
        for name in ("test.log","test.xml"):
            path = Path("bazel-testlogs/runsc/gofer/net/netgofer_test") / name
            if path.is_file() and path.stat().st_size <= 16*1024*1024:
                (dest / name).write_bytes(path.read_bytes())


def project_log(raw):
    framed = raw.count(b"\x16")
    lines = raw.replace(b"\x16",b"").decode(errors="replace").splitlines()
    starts, ends, active = [], [], []
    for line in lines:
        m = re.match(r"^\s*(=== RUN\s+|--- (?:PASS|FAIL|SKIP):\s+)([A-Za-z0-9_/]+)(?:\s|$)", line)
        if m and m[2] in CASES:
            if m[1].startswith("==="):
                starts.append(m[2])
            else:
                ends.append([m[2], m[1].split()[1].rstrip(":")])
        m = re.fullmatch(r"\s*([A-Za-z0-9_/]+) \([0-9hms.]+\)",line)
        if m and m[1] in CASES:
            active.append(m[1])
    hashes = re.findall(r"^NATIVE_TEST_SHA256=([a-f0-9]{64})$", "\n".join(lines), re.M)
    return {"framing_bytes":framed, "started":starts[-24:], "finished":ends[-24:], "running":active[:12],
            "go_timeout":any("panic: test timed out after" in line for line in lines),
            "stacks":stack_frames(lines),"native_amd64": "NATIVE_WORKER=x86_64" in lines,
            "amd64_elf":bool(re.search(r"^NATIVE_MACHINE=\s*62\s*$","\n".join(lines),re.M)),
            "test_sha256":hashes[0] if len(hashes)==1 else None}


def file_hash(item):
    digest=item.get("digest","")
    if isinstance(digest,dict):
        digest=digest.get("hash","")
    if m:=re.search(r"/(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/[0-9]+(?:$|\?)",item.get("uri","")):
        return m[1]
    return digest if re.fullmatch(r"[a-f0-9]{64}",digest) else None


def case_duration(event):
    if v:=event.get("testAttemptDurationMillis"):
        return int(v)
    m=re.fullmatch(r"(\d+(?:\.\d+)?)s",str(event.get("testAttemptDuration","")))
    return round(float(m[1])*1000) if m else None


def physical(db):
    query = '''select(.spawn and .spawn.mnemonic=="TestRunner" and ((.spawn.targetLabel//"")|sub("^@@";""))==$owner) |
       .spawn | ((.platform.properties//[])|map({key:.name,value:.value})|from_entries) as $p |
       {remote:(.runner=="remote"), cache:(.cacheHit//false), amd64:($p.Arch=="amd64"), oci:($p["workload-isolation-type"]=="oci"), image:($p["container-image"]==$image), action_exit:(.exitCode//0)}'''
    with subprocess.Popen([str(WORK/"bb"),"print","--raw","--compact_execution_log="+str(db)],
                          stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as decode:
        out=subprocess.run(["jq","-c","--arg","owner",TARGET,"--arg","image",IMAGE,query],
                           stdin=decode.stdout,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        decode.stdout.close()
        if decode.wait() or out.returncode:
            return []
    return [json.loads(v) for v in out.stdout.splitlines()][:4]


def collect(dest):
    row=json.loads((dest/"timing.json").read_text()) if (dest/"timing.json").exists() else {}
    results=[]
    targets=set()
    bep=dest/"bep"
    if bep.exists():
        with bep.open() as events:
            for line in events:
                e=json.loads(line)
                ident=e.get("id",{})
                if "started" in e: row["invocation"]=str(uuid.UUID(e["started"]["uuid"]))
                if "finished" in e: row["bep_exit"]=int(e["finished"]["exitCode"].get("code",0))
                if "testResult" in e and "testResult" in ident:
                    t=e["testResult"];label=ident["testResult"]["label"]
                    targets.add(label)
                    if label==TARGET:
                        results.append(t)
    row["only_owner"]=targets=={TARGET}
    row["test_results"]=len(results)
    if len(results)!=1:
        return row
    t,=results
    status=t.get("status","NO_STATUS")
    row["status"]=status if status in {"PASSED","FAILED","TIMEOUT","INCOMPLETE","REMOTE_FAILURE","FAILED_TO_BUILD"} else "OTHER"
    row["test_ms"]=case_duration(t)
    info=t.get("executionInfo",{})
    row.update(remote=info.get("strategy")=="remote",cached=bool(t.get("cachedLocally") or info.get("cachedRemotely")))
    for item in t.get("testActionOutput",[]):
        name=item.get("name")
        if name not in ("test.log","test.xml"):continue
        sha=file_hash(item)
        f=dest/name
        raw=f.read_bytes() if f.exists() else None
        row[name]={"sha256":sha, "verified":raw is not None and hashlib.sha256(raw).hexdigest()==sha}
        if not row[name]["verified"]:continue
        if name=="test.log":
            row["log"]=project_log(raw)
        else:
            try:
                row["cases"] = [[case_name,"FAIL" if c.find("failure") is not None or c.find("error") is not None else
                                 "SKIP" if c.find("skipped") is not None or c.get("status")=="notrun" else "PASS"]
                                for c in ET.fromstring(raw).iter("testcase")
                                if (case_name:=c.get("name","").split(".",1)[-1]) in CASES][:24]
            except ET.ParseError:
                row["xml_malformed"]=True
    if (dest/"compact").is_file():
        row["workers"]=physical(dest/"compact")
    return row


def report():
    assert "BUILDBUDDY_API_KEY" not in os.environ
    data={"source":SOURCE,"target":TARGET,"old_invocation":INV,"runs":[]}
    old=WORK/"original.log"
    if old.exists():
        b=old.read_bytes()
        assert len(b)==1274 and hashlib.sha256(b).hexdigest()==ORIGINAL
        data["original"]={"sha256":ORIGINAL,"bytes":len(b),"log":project_log(b)}
    for i in range(1,6):
        try:
            row=collect(WORK/str(i))
        except (OSError,KeyError,ValueError,AssertionError,TypeError) as e:
            row={"report_error":type(e).__name__}
        data["runs"].append(row)
    def observed(row):
        workers=row.get("workers",[])
        return (row.get("only_owner") and row.get("remote") and not row["cached"] and row.get("exit")==row.get("bep_exit")
                and row.get("test_ms") is not None and row.get("test.log",{}).get("verified") and
                row.get("log",{}).get("native_amd64") and row["log"].get("amd64_elf") and row["log"].get("test_sha256") and
                len(workers)==1 and all(workers[0].get(k) for k in ("remote","amd64","oci","image")) and not workers[0]["cache"])
    data["complete"]=bool("original" in data and all(observed(r) for r in data["runs"]))
    data["all_passed"]=data["complete"] and all(r.get("status")=="PASSED" and r.get("exit")==0 and
        {n for n,v in r.get("cases",[]) if v=="PASS"}==CASES for r in data["runs"])
    encoded=json.dumps(data,indent=2).encode()
    assert len(encoded)<250000
    (WORK/"receipt.json").write_bytes(encoded+b"\n")


if __name__ == "__main__":
    {"original":original,"run":run,"report":report}[sys.argv[1]]()
