"""Compare TCP segmentation and syscall returns across the same private veth fixture."""
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(os.environ["RUNNER_TEMP"])
ARM = sys.argv[1] if len(sys.argv) == 2 else None
assert ARM in (None, "baseline", "candidate")
WORK = ROOT / "mtu-wire" / (ARM or "coordinator")
OUT = ROOT / "mtu-wire/results" / (ARM or "coordinator")
SHARED = ROOT / "packet-worker"
DONOR = "7344701fde31934f4a08c9163a2285611c13784c"
BASE = "a4ca95b85a9a770e6e01852c30da57ae94f89b52"
PRODUCT = "fc6a9528a4097da9c18f0a23ebadb1e6728b4682" if ARM == "candidate" else BASE
TREE = "3c22d406d6ca5dc976055e79d153ef244b79c9a7" if ARM == "candidate" else "267a84bed95072815c9b967d36dc1044bf2ef18a"
FIXTURE = Path("test/tcp_path_mtu_qualification")
CASE = "host/v6/mss_0/on"
CASES = [CASE] if ARM != "candidate" else [CASE, "host/v6/mss_0/off", "host/v4/mss_0/on"]
TEST = "TestExperimentMTUWire"
DIGEST = "2bbe80677e4b0a1bc6f54540c2a5897fbf1d55c08b126fb4cc46aa2446dc526d"
RESULT = {"verified": False, "carrier": os.environ["GITHUB_SHA"], "cases": CASES, "arm": ARM, "data": {},
          "source": PRODUCT, "tree": TREE, "donor": DONOR, "original": 13605261310,
          "previous_delivery": 13622937557}
sys.path[:0] = [str(WORK), str(SHARED)]

def git(*args):
    return subprocess.check_output(["git", *args])

def save():
    (OUT / "veth-leaf.json").write_text(json.dumps(RESULT, indent=2, sort_keys=True)+"\n")

def replace_one(code, old, new):
    assert code.count(old) == 1, old
    return code.replace(old, new)

def prepare():
    assert os.uname().sysname == "Linux" and os.uname().machine == "x86_64"
    assert git("rev-parse", PRODUCT+"^{tree}").decode().strip() == TREE
    assert git("rev-parse", PRODUCT+"^").decode().strip() == (BASE if ARM == "candidate" else "67c6038796bc6f70215e32699744843e3f10317f")
    bazel = SHARED / "bazel"
    assert hashlib.sha256(bazel.read_bytes()).hexdigest() == "7f775b54bbec487efaf0f49ccda1d2ed02cd34f582edfdd6aeeec76e39ef585c"
    (WORK / "bazel").symlink_to(bazel)
    for name in ("BUILD","wire_test.go","netns.sh","decode.py","qualify.py","stock.json"):
        data = git("show", DONOR+":.github/qualification/tcp_path_mtu/"+name)
        (WORK / name).write_bytes(data)
        if name in ("BUILD", "wire_test.go"):
            (FIXTURE / name).write_bytes(data)
    wrapper = WORK / "netns.sh"
    wrapper.write_text(replace_one(wrapper.read_text(), "strace -ff -qq -ttt -s 16",
                                   "strace -ff -qq -ttt -T -s 16"))
    wrapper.chmod(0o700)
    # Keep the donor's original case indices, packet decoder, syscall joins,
    # BEP-bound output copying, and checks. Limit each arm to its specified leaves.
    code = (WORK / "qualify.py").read_text()
    code = replace_one(code, 'if mode=="host"}', 'if f"{mode}/{family}/mss_{mss}/{phase}" in wanted_cases}')
    old_filter = '"--test_arg=-test.run=^TestExperimentMTUWire$/^host$"'
    if ARM == "candidate":
        selector = ('"--test_arg=-test.run=^TestExperimentMTUWire$/^host$/^(v4|v6)$/^mss_0$/^(on|off)$",'
                    '"--test_arg=-test.skip=^TestExperimentMTUWire$/^host$/^v4$/^mss_0$/^off$"')
    else:
        selector = '"--test_arg=-test.run=^TestExperimentMTUWire$/^host$/^v6$/^mss_0$/^on$"'
    code = replace_one(code, old_filter, selector)
    code = replace_one(code, '"--lockfile_mode=off",', '"--lockfile_mode=off",*distdirs,')
    (WORK / "qualify_one.py").write_text(code)

def sendmmsg_returns():
    # -ff makes each file specific to one TID. A resumed syscall retains its
    # original entry timestamp and fd; store only its return and duration.
    returns = {}
    for path in WORK.glob("syscall.*"):
        pending = None
        for line in path.read_text(errors="replace").splitlines():
            start = re.match(r"^(\d+)[.](\d{6}) sendmmsg\((\d+),", line)
            if start:
                key = (int(start[3]), int(start[1])*1_000_000+int(start[2]))
                if "<unfinished ...>" in line:
                    pending = key
                    continue
            elif re.match(r"^\d+[.]\d+ <[.][.][.] sendmmsg resumed>", line) and pending:
                key, pending = pending, None
            else:
                continue
            parts = re.search(r"\)\s+=\s+(.{1,120}?)\s+<(\d+)[.](\d{6})>$", line)
            if not parts:
                continue
            raw = parts[1]
            m = re.fullmatch(r"(-?\d+)(?: ([A-Z][A-Z0-9_]+) \([^)]+\))?", raw)
            duration = int(parts[2])*1_000_000+int(parts[3])
            assert key not in returns
            returns[key] = {"entry_us": key[1], "duration_us": duration, "return_text": raw,
                            "return": int(m[1]) if m else None, "errno_name": m[2] if m else None,
                            "errno_number": getattr(errno,m[2],None) if m and m[2] else None}
    return returns

def observe_calls(reader, item, case):
    cell = RESULT["data"][case] = {}
    headers = reader.syscall_headers()
    trace = sendmmsg_returns()
    result = cell["data_sendmmsg"] = []
    cell.update(kernel=os.uname().release, source_fd=item["SourceFD"],
                  read=item["Read"], application_sha256=item["Digest"])
    for call in item["Calls"]:
        match = [v for (fd, timestamp),v in trace.items()
                 if fd == item["SourceFD"] and call["BeginUS"] <= timestamp <= call["EndUS"]]
        row = {"begin_us":call["BeginUS"], "end_us":call["EndUS"], "sent":call["Sent"],
               "link_error":call["Error"], "packets":call["Packets"], "returns":match,
               "virtio_calls":[h for h in headers if h["fd"] == item["SourceFD"] and
                               call["BeginUS"] <= h["us"] <= call["EndUS"]]}
        result.append(row)
    save()
    for row in result:
        syscall, = row["returns"]
        header, = row["virtio_calls"]
        assert header["call"] == "sendmmsg" and header["fd"] == item["SourceFD"]
        assert header["us"] == syscall["entry_us"] and header["virtio"]["gso_type"] in (0,1,4)
        packet, = row["packets"]
        expected_type = 4 if item["Family"] == "v6" else 1
        assert header["virtio"]["gso_type"] == (expected_type if packet["Payload"] > packet["GSO"]["MSS"] else 0)
        if syscall["return"] == -1:
            assert row["sent"] == 0 and row["link_error"] and syscall["errno_number"] is not None
        else:
            assert syscall["return"] == row["sent"] == 1 and not row["link_error"] and not syscall["errno_name"]
    aggregate = [r for r in result if r["virtio_calls"][0]["virtio"]["gso_type"]]
    errors = [r for r in result if r["sent"] == 0]
    cell.update(aggregates=len(aggregate), errors=len(errors),
                rejected_aggregates=sum(r["sent"] == 0 for r in aggregate))
    save()
    if ARM == "baseline":
        assert aggregate
    elif case == CASE:
        assert result and not aggregate and not errors
    else:
        assert aggregate and not errors

def check(reader):
    receipt = reader.REPORT
    assert receipt["verified"] and set(receipt["cases"]) == set(CASES)
    for case in CASES:
        item = receipt["cases"][case]
        cell = RESULT["data"][case]
        assert item["verified"] and item["Outcome"] == "delivered" and not item["StageError"]
        assert item["Wrote"] == item["Read"] == item["matched_peer_bytes"] == item["stream_bytes"] == 10153
        assert item["Digest"] == item["Expected"]
        if case == CASE:
            assert item["Digest"] == DIGEST
        ipv6, enabled = item["Family"] == "v6", item["Phase"] == "on"
        assert item["SelectedGSO"] == item["Vnet"] == 1 and item["negotiated_peer_mss"] == (1440 if ipv6 else 1460)
        network_header = (40 if ipv6 else 20) + ((8 if ipv6 else 4) if enabled else 0)
        assert item["joined_checksum_starts"].get(network_header,0) > 0
        assert not item["Frames_summary"]["reassembled"]
        frames = item["Frames_summary"]["frames"]
        assert frames and all(0 < p["ip_bytes"] <= 1500 and not p["frag_offset"] and not p["more"] for p in frames)
        data = [p for p in frames if p.get("tcp",{}).get("data")]
        assert data and all(p["option"] == (99 if enabled else 0) and p["tcp"]["checksum_valid"] and p["tcp"]["payload_matches"] for p in data)
        cell.update(sha256=item["Digest"],peer_data_frames=len(data),
                    max_ip_bytes=max(p["ip_bytes"] for p in data), validated_checksum_starts=item["joined_checksum_starts"])
    assert len(receipt["initial-links"]) == 1 and "lo:" in receipt["initial-links"][0]
    assert len(receipt["links"]) == 3 and all(any(dev in l and "mtu 1500 " in l for l in receipt["links"]) for dev in ("mtua@","mtub@"))
    assert int(receipt["outer-uid"][0]) != 0
    logs = (OUT / "test.log").read_text().replace("\x16","")
    assert sorted(re.findall(r"(?m)^\s*--- PASS: (\S+) [(]",logs)) == sorted([TEST]+[TEST+"/"+c for c in CASES])
    xml = ET.parse(OUT / "test.xml")
    names = [e.get("name") for e in xml.iter("testcase")]
    assert sorted(n for n in names if n != TEST) == sorted(TEST+"/"+c for c in CASES)
    assert not any(e.tag in ("failure","error","skipped") for e in xml.iter())
    events = [json.loads(line) for line in (WORK / "bep").read_bytes().splitlines() if line]
    assert any((i:=e["id"].get("targetCompleted",{})).get("label","").removeprefix("@@") == reader.OWNER
               and i.get("configuration",{}).get("id") == receipt["test"]["configuration"] for e in events)
    RESULT.update(verified=True,test=receipt["test"])

def main():
    WORK.mkdir(parents=True,exist_ok=False)
    OUT.mkdir(parents=True,exist_ok=True)
    save()
    start = git("rev-parse","HEAD").decode().strip()
    assert not git("diff","HEAD","--name-only")
    git("switch","--quiet","--detach",PRODUCT)
    made_fixture = False
    try:
        assert not FIXTURE.exists()
        FIXTURE.mkdir()
        made_fixture = True
        prepare()
        import qualify_one as reader
        import timer_teardown as public
        reader.W, reader.OUT, reader.wanted_cases = WORK, OUT, CASES
        reader.SOURCE = PRODUCT
        reader.REPORT["source"] = PRODUCT
        public.assets.DIST.mkdir(parents=True,exist_ok=True)
        public.known.update((r[2],r) for r in json.loads((SHARED/"inputs.json").read_bytes()))
        public.dirs.update(p for p in (public.assets.DIST/"content").glob("*") if p.is_dir())
        end = time.monotonic()+15*60
        for attempt in range(8):
            reader.distdirs = ["--distdir="+str(p) for p in sorted(public.dirs)]
            bep = WORK / "bep"
            bep.unlink(missing_ok=True)
            try:
                try:
                    reader.main()
                finally:
                    if (OUT / "test.log").is_file() and (OUT / "test.xml").is_file():
                        records = re.findall(r"(?m)MTU_RESULT (\{.*\})$", (OUT / "test.log").read_text())
                        for record in records:
                            item = json.loads(record)
                            case = f"{item['Mode']}/{item['Family']}/mss_{item['PeerMSS']}/{item['Phase']}"
                            assert case in CASES and case not in RESULT["data"]
                            observe_calls(reader, item, case)
                        assert set(RESULT["data"]) == set(CASES)
                check(reader)
                break
            except Exception as exc:
                console = WORK / "bazel.log"
                log = console.read_text(errors="replace") if console.exists() else ""
                RESULT["last_attempt"] = {"attempt": attempt+1,"error":repr(exc)[:300],
                    "diagnostics":[l[:400] for l in log.splitlines() if l.startswith("ERROR:")][-10:]}
                save()
                # A completed test (pass or failure) is never repeated.
                if bep.is_file() and b'"testResult"' in bep.read_bytes():
                    raise
                if time.monotonic() >= end or not public.acquire(log):
                    raise
        assert RESULT["verified"]
    finally:
        if made_fixture:
            shutil.rmtree(FIXTURE)
        git("switch","--quiet","--detach",start)

if __name__ == "__main__":
    if ARM is None:
        results = [subprocess.run([sys.executable,"-B",__file__,arm]).returncode for arm in ("baseline","candidate")]
        raise SystemExit(any(results))
    try:
        main()
    except Exception as error:
        import traceback
        RESULT["verified"] = False
        RESULT["error"] = repr(error)[:450]
        RESULT["line"] = traceback.extract_tb(error.__traceback__)[-1].lineno
    save()
    raise SystemExit(not RESULT["verified"])
