"""Compare Reno and CUBIC through the same interrupted, timestamp-free SACK recovery."""
import hashlib
import json
import os
import subprocess
from pathlib import Path
import xml.etree.ElementTree as ET


OWNER = "//pkg/tcpip/tests/integration:virtual_tcp_test"
ROOT = "TestVirtualTCPInterruptedSACK"
BODY = bytes((i * 17 + i // 251) % 256 for i in range(512 << 10))
RTO_RECOVERY = 1
SACK_RECOVERY = 3


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def ahead(seq: int, base: int) -> bool:
    return 0 < (seq - base) % 2**32 < 2**31


def sparse(ack: int, blocks: list | None) -> bool:
    return any(ahead(b["Start"], ack) and ahead(b["End"], b["Start"]) for b in blocks or [])


def point_row(e, t0, seq):
    mod = 2**32
    return {'ms': (e['At']-t0)/1e6, 'una': (e['UNA']-seq)%mod, 'nxt': (e['NXT']-seq)%mod,
            'state': e['State'], 'active':e['Active'], 'cwnd':e['Cwnd'], 'threshold':e['Threshold'], 'outstanding':e['Flight']}


def measure(samples):
    if set(samples) != {ROOT + "/timer_before", ROOT + "/timer_after"}:
        raise ValueError("Expected both actual reverse ACK schedules")
    rows = {}
    for name, sample in samples.items():
        path = name.rsplit("/", 1)[-1]
        ev = sample["Events"]
        wire = [e["Wire"] for e in ev if "Wire" in e]
        data = [w for w in wire if w["From"] == 0 and w["Bytes"]]
        sender = [(i, e["Sender"]) for i, e in enumerate(ev) if "Sender" in e]
        seq, t0, mss = data[0]["Seq"], sample["FirstData"], data[0]["Bytes"]
        release = {"timer_before":218, "timer_after":224}[path]
        if (sample["Algorithm"], sample["KernelOS"], sample["KernelArch"], sample["Go"]) != ("reno", "linux", "amd64", "go1.27.3"):
            raise ValueError("Wrong native endpoint/toolchain identity")
        if [(p["From"],p["Flags"],p["Permitted"]) for p in wire if p["Flags"]&2] != [(0,2,True),(1,18,True)] or any(p["TS"] for p in wire) or any(e["Timestamps"] or not e["SACKEnabled"] for _,e in sender):
            raise ValueError("Timestamp/SACK contract violated")
        if not (sample["Accepted"] == sample["Offered"] == len(BODY) and sample["BodyHash"] == hashlib.sha256(BODY[:sample["Delivered"]]).hexdigest()):
            raise ValueError("Incorrect sender/receiver payload")
        for i,w in enumerate(data):
            pos = (w["Seq"]-seq)%2**32
            expected_drop = w["At"]-t0 < 150_000_000 and (w["Retry"] or pos not in (0, 8*mss, 9*mss, 11*mss))
            if w["Dropped"] != expected_drop or w["Offer"] != i+1:
                raise ValueError("Changing forward loss conditions")
        held = [w for w in wire if w["Held"]]
        if len(held) != 1 or held[0]["From"] != 1 or held[0]["Due"] != t0+release*1_000_000 or not any((z["End"]-seq)%2**32==12*mss for z in held[0]["SACK"]) or any(w["Dropped"] for w in wire if w["From"]==1):
            raise ValueError("The intended physical ACK was not held/delivered exactly once")
        def compact(i,e):
            d = {"i":i,"kind":e["Kind"],**point_row(e,t0,seq),"target_ms":(e["TimerTarget"]-t0)/1e6,"rack_rtt_ms":e["RackRTT"]/1e6,"reo_ms":e["ReoWnd"]/1e6,"loss_count":e["Lost"]}
            if e["Kind"] in ("rack_mark","marked_repair"):
                d.update(offset=(e["Segment"]-seq)%2**32,bytes=e["SegmentBytes"])
            return d
        rt = [(i,e) for i,e in sender if e["Kind"]=="rto_interrupt" and e["State"]==SACK_RECOVERY and e["Active"]]
        holds = [(i,e) for i,e in sender if e["Kind"]=="ack_begin" and e["At"]==held[0]["Due"] and e["ACK"]==held[0]["ACK"] and e["SACK"]==held[0]["SACK"]]
        if len(holds)!=1: raise ValueError("Missing correctly joined receiver wire ACK")
        h=holds[0][0]; last = next(i for i,e in sender if i>h and e["Kind"]=="ack_end")
        held_actions = [compact(i,e) for i,e in sender if h<=i<=last]
        arms = [(i,e) for i,e in sender if h<=i<=last and e["Kind"]=="timer_arm"]
        timers = []
        for i,e in sender:
            if e["Kind"]!="timer_expired":continue
            stop = next(j for j,f in sender if j>i and f["Kind"]=="timer_return")
            timers.append({"fire":compact(i,e),"during":[compact(j,f) for j,f in sender if i<j<stop]})
        matched=[t for t in timers if any(t["fire"]["target_ms"]==compact(i,e)["target_ms"] for i,e in arms)]
        first_rt = compact(*rt[0]) if rt else None
        complete = sample["EOF"] and sample["FinACK"] and not sample["Pending"] and sample["Acknowledged"]==sample["Delivered"]==len(BODY)
        if not complete and sample["End"]-t0!=35_000_000_000:raise ValueError("Incomplete trial ended prior to diagnostic deadline")
        row={"path":path,"complete":complete,"acked":sample["Acknowledged"],"delivered":sample["Delivered"],"body_sha256":sample["BodyHash"],"eof":sample["EOF"],"fin_ack":sample["FinACK"],"pending":sample["Pending"],"ms":(sample["End"]-t0)/1e6,"rtos":sample["RTOs"],"retransmits":sample["Retransmissions"],"sack_episodes":sample["SACKRecoveries"],"held_ack_sha256":held[0]["Hash"],"held_ack":(held[0]["ACK"]-seq)%2**32,"held_sack":[[(b["Start"]-seq)%2**32,(b["End"]-seq)%2**32] for b in held[0]["SACK"]],"held_ack_actions":held_actions,"first_interrupted_rto":first_rt,"target_expirations":matched,"native_timers":timers,"early_wire":[{"i":i,"ms":(p["At"]-t0)/1e6,"arrives_ms":(p["Due"]-t0)/1e6,"offset":(p["Seq"]-seq)%2**32,"count":p["Bytes"],"retry":p["Retry"],"drop":p["Dropped"],"sha256":p["Hash"]} for i,x in enumerate(ev) if (p:=x.get("Wire")) and p["From"]==0 and p["Bytes"] and p["At"]-t0<=500_000_000],"events_sha256":digest(ev)}
        if len(matched)==1 and first_rt is not None:
            fire=matched[0]["fire"]
            row["enrollment"] = ("before timeout" if fire["i"]<first_rt["i"] else "across timeout" if arms[0][0]<first_rt["i"] else "armed after timeout")
        else:
            row["enrollment"]="no matching expiration"
        rows[path]=row
    return rows


def native_results(directory: Path, phase: str, owner: str, runs: int) -> tuple[dict, dict]:
    manifest = json.loads((directory / "events.jsonl.manifest.json").read_text())
    event_body = (directory / "events.jsonl").read_bytes()
    if (len(event_body) != manifest["events"]["bytes"]
            or hashlib.sha256(event_body).hexdigest() != manifest["events"]["sha256"]):
        raise ValueError(f"{phase}: native event identity mismatch")
    native = {}
    for event in map(json.loads, event_body.splitlines()):
        if "testResult" in event["id"]:
            if event["id"]["testResult"]["label"] != owner:
                raise ValueError(f"{phase}: unexpected native test owner")
            key = json.dumps(event["id"]["testResult"], sort_keys=True)
            value = event["testResult"]
            if (key in native or value["status"] != "PASSED" or value.get("cachedLocally")
                    or value.get("executionInfo", {}).get("cachedRemotely")
                    or value.get("executionInfo", {}).get("strategy") != "remote"):
                raise ValueError(f"{phase}: native attempt identity, freshness or execution strategy differs")
            native[key] = value
    if len(native) != runs:
        raise ValueError(f"{phase}: native attempt census differs")
    return manifest, native


def observations(evidence: Path, phase: str, runs: int) -> dict:
    owner = OWNER
    directory = evidence / ("invocation-" + phase)
    verdict = json.loads((directory / "verdict.json").read_text())
    executed = verdict["executed"]
    if verdict["status"] != "PASS" or len(executed) != runs:
        raise ValueError(f"{phase}: expected {runs} passing native attempts")
    if {row["run"] for row in executed} != set(range(1, runs + 1)):
        raise ValueError(f"{phase}: incomplete run census")
    for row in executed:
        if (row["owner"]["label"] != owner or row["status"] != "PASSED"
                or row["cached_locally"] or row["attempt"] != 1 or row["shard"] != 1):
            raise ValueError(f"{phase}: unexpected attempt identity: {row}")
    manifest, native = native_results(directory, phase, owner, runs)
    samples = {}
    xml_runs = {}
    references = []
    paths = set()
    executed_keys = {(row["owner"]["label"], row["owner"]["configuration"], row["run"],
                      row["attempt"], row["shard"]) for row in executed}
    for row in manifest["reports"]:
        identity = row["identity"]
        key = (identity["label"], identity["configuration"]["id"], identity["run"],
               identity["attempt"], identity["shard"])
        if key not in executed_keys:
            raise ValueError(f"{phase}: report does not match an executed attempt")
        outputs = native[json.dumps(identity, sort_keys=True)]["testActionOutput"]
        matches = [item for item in outputs if item["name"] == row["name"]]
        if not row["path"].startswith("reports/") or len(matches) != 1 or not matches[0]["uri"].endswith("/" + row["path"][len("reports/"):]):
            raise ValueError(f"{phase}: report has no unique native output")
        path = directory / row["path"]
        if path in paths:
            raise ValueError(f"{phase}: attempts share a retained report path")
        paths.add(path)
        body = path.read_bytes()
        if len(body) != row["bytes"] or hashlib.sha256(body).hexdigest() != row["sha256"]:
            raise ValueError(f"{phase}: report hash mismatch")
        run = identity["run"]
        if row["name"] == "test.xml":
            cases = ET.fromstring(body).findall(".//testcase")
            names = [case.attrib["name"] for case in cases]
            if (len(names) != len(set(names)) or ROOT not in names or any(list(case) for case in cases)
                    or run in xml_runs):
                raise ValueError(f"{phase}: unexpected XML cases {names}")
            xml_runs[run] = set(names) - {ROOT}
        elif row["name"] == "test.log":
            parts = {}
            expected = {}
            for line in body.decode().splitlines():
                if "VIRTUAL_TCP_PART " not in line:
                    continue
                name, ordinal, chunk = line.split("VIRTUAL_TCP_PART ", 1)[1].split(" ", 2)
                index, count = map(int, ordinal.split("/"))
                rows = parts.setdefault(name, {})
                if not 0 <= index < count or index in rows or count != expected.setdefault(name, count):
                    raise ValueError(f"{phase}: duplicate or malformed native trace chunks")
                rows[index] = chunk
            values = []
            for name, rows in parts.items():
                value = json.loads("".join(rows[index] for index in range(expected[name])))
                if name != value["Case"]:
                    raise ValueError(f"{phase}: native trace chunk identity differs")
                values.append(value)
            cases = {value["Case"]: value for value in values}
            if len(cases) != len(values) or run in samples or any(not name.startswith(ROOT + "/") for name in cases):
                raise ValueError(f"{phase}: invalid virtual transfer identities")
            samples[run] = cases
        else:
            raise ValueError(f"{phase}: unexpected report {row['name']}")
        references.append(row)
    if set(samples) != set(range(1, runs + 1)) or set(xml_runs) != set(samples):
        raise ValueError(f"{phase}: log/XML census differs")
    baseline = samples[1]
    for run, sample in samples.items():
        if xml_runs[run] != set(sample) or sample != baseline:
            raise ValueError(f"{phase}: case identity or complete virtual observations differ in run {run}")
    return {"_samples": baseline, "attempts": runs, "measurements": measure(baseline), "reports": references,
            "complete_observation_sha256": digest(baseline)}


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    child, fixed, stock = (config[k] for k in ("source", "fixed", "stock"))
    if (config["targets"] != [OWNER] or config["expected_sources"] != [child, fixed, stock]
            or subprocess.check_output(["git", "rev-parse", child + "^", child + "^^"], text=True).splitlines() != [fixed, stock]):
        raise ValueError("Unexpected source or native owner")
    options = ["--config=x86_64", "--compilation_mode=opt", *config["consumer_options"], "--lockfile_mode=error",
               "--remote_download_outputs=all", "--noannounce_rc"]
    # This revision's formatter is goimports -format-only. Use Bazel's pinned
    # target directly so it inherits the qualification's authenticated config.
    changed_go = ["pkg/tcpip/tests/integration/reno_burst_test.go",
                  *(f"pkg/tcpip/transport/tcp/{name}.go" for name in ("connect", "protocol", "rack", "snd", "reno", "cubic"))]
    formatted = invoke("goimports", ["run", *options, "--run_in_cwd", "@org_golang_x_tools//cmd/goimports", "--",
                                     "-format-only", "-d", *changed_go], "command", targets=[])
    if formatted["status"] != "PASS" or (evidence / "invocation-goimports/stdout.log").stat().st_size:
        raise RuntimeError("Canonical goimports check failed")
    selected = ["--strategy=TestRunner=remote", "--nocache_test_results", "--flaky_test_attempts=1",
                "--test_output=errors", "--test_timeout=300", "--test_env=GO_TEST_WRAP=1",
                "--test_env=GO_TEST_WRAP_TESTV=1", "--runs_per_test=2", f"--test_arg=-test.run=^{ROOT}$"]
    result = {"status": "INCOMPLETE", "sources": config["expected_sources"], "phases": {}}
    full_samples = {}
    try:
        for label, rev in (("stock", stock), ("boundary", fixed), ("suppress", child)):
            subprocess.run(["git", "switch", "--detach", rev], check=True)
            phase = invoke(label, ["test", *options, *selected, OWNER], "test", targets=[OWNER], source=rev)
            if phase["source"] != rev:
                raise RuntimeError(f"{label}: native source differs")
            if phase["status"] != "PASS":
                result["phases"][label] = {"source": rev, "error": "native target failed"}
                continue
            try:
                obs = observations(evidence, label, 2)
                full_samples[label] = obs.pop("_samples")
                result["phases"][label] = {"source": rev, **obs}
            except (ValueError, KeyError, OSError) as error:
                result["phases"][label] = {"source": rev, "error": str(error)}
        if any("error" in r for r in result["phases"].values()):
            raise RuntimeError("One or more exact-source comparisons failed")
        for path in ("timer_before","timer_after"):
            arms = [r["measurements"][path] for r in result["phases"].values()]
            if len({r["held_ack_sha256"] for r in arms})!=1:
                raise RuntimeError("Mismatched receiver ACK on matched path")
        result["enrolled"] = all(p["measurements"]["timer_after"]["enrollment"]=="across timeout" and p["measurements"]["timer_before"]["enrollment"]=="before timeout" for p in result["phases"].values())
        result["status"] = "PASS"
    finally:
        subprocess.run(["git", "switch", "--detach", child], check=True)
        (evidence / "rack-reorder-timer.json").write_text(json.dumps(result, indent=2) + "\n")
