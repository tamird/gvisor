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


def summarize(s, algorithm, schedule):
    ev = s['Events']; t0=s['FirstData']; w=[e['Wire'] for e in ev if 'Wire' in e]; states=[e['Sender'] for e in ev if 'Sender' in e]; n=len(BODY)
    spec = {'none':(0,0),'interrupted_sack':(2,8)}[schedule]
    if ((s['Algorithm'],s['KernelOS'],s['KernelArch'],s['Go']) != (algorithm,'linux','amd64','go1.27.3') or
        s['Schedule'] != {'Name':schedule,'First':spec[0],'Last':spec[1]} or s['Offered']!=n or s['Accepted']!=n or
        not 0<=s['Delivered']<=n or not 0<=s['Acknowledged']<=n or
        s['BodyHash'] != hashlib.sha256(BODY[:s['Delivered']]).hexdigest()):
        raise ValueError(f"{s['Case']}: endpoints or receiver bytes differ")
    if [(p['From'],p['Flags'],p['Permitted']) for p in w if p['Flags']&2] != [(0,2,True),(1,18,True)] or any(p['TS'] for p in w) or any(e['Timestamps'] or not e['SACKEnabled'] for e in states):
        raise ValueError('incorrect SACK/timestamp negotiation')
    data=[p for p in w if p['From']==0 and p['Bytes']]; rev=[p for p in w if p['From']==1]; seq=data[0]['Seq']; mss=data[0]['Bytes']; mod=2**32
    if len(data)!=s['Offers'] or [p['Offer'] for p in data]!=list(range(1,len(data)+1)) or t0!=data[0]['At'] or len(data)<10:
        raise ValueError('incomplete native forward wire offers')
    if any((p['Seq']-seq)%mod!=i*mss or p['Bytes']!=mss or p['Retry'] for i,p in enumerate(data[:10])):
        raise ValueError('first flight differs')
    for p in data:
        pos=(p['Seq']-seq)%mod
        expect=(schedule=='interrupted_sack' and p['At']-t0<150_000_000 and (p['Retry'] or not (pos==0 or 8*mss<=pos<10*mss)))
        if p['Dropped']!=expect:raise ValueError('physical time/sequence drop policy differs')
    if any(p['Dropped'] for p in rev) or s['Drops']!=sum(p['Dropped'] for p in data):raise ValueError('drop count or reverse delivery differs')
    complete=s['EOF'] and s['FinACK'] and s['Pending']==0; elapsed=s['End']-t0
    if not 0<=elapsed<=35_000_000_000 or (complete and (s['Acknowledged'],s['Delivered'])!=(n,n)) or (not complete and elapsed!=35_000_000_000):raise ValueError('invalid virtual deadline/closure')
    loss=algorithm+'_loss_'; timeout=algorithm+'_timeout_';
    def callback_pairs(prefix):
        observed=[(i,e) for i,e in enumerate(states) if e['Kind'].startswith(prefix)]
        if len(observed)%2:raise ValueError('missing controller call endpoint')
        pairs=[]
        for (i,a),(j,b) in zip(observed[::2],observed[1::2]):
            if (a['Kind'],b['Kind'],a['At'])!=(prefix+'begin',prefix+'end',b['At']) or j!=i+1:raise ValueError('unpaired controller call')
            pairs.append((i,a,b))
        return pairs
    losses=callback_pairs(loss);timeouts=callback_pairs(timeout)
    if sum(e['Kind']=='rto_interrupt' for e in states)!=s['RTOs'] or len(timeouts)!=s['RTOs'] or sum(e['Kind']=='rto_enter' for e in states)!=s['RTOs']:
        raise ValueError('actual timeout/controller counter differs')
    def cb(a,b): return {'before':point_row(a,t0,seq),'after':point_row(b,t0,seq)}
    def ack_wire(a):
        matches=[p for p in rev if p['ACK']==a['ACK'] and p['Due']==a['At'] and p['SACK']==a['SACK'] and not p['Dropped']]
        if len(matches)!=1:raise ValueError('ambiguous receiving ACK on simulated wire')
        return matches[0]
    def ack_end(i,a):
        z=next((e for e in states[i+1:] if e['Kind'] in ('ack_begin','ack_end')),None)
        if z is None or z['Kind']!='ack_end' or z['ACK']!=a['ACK']:raise ValueError('unpaired receiver ACK')
        return z
    def evidence(i,a):
        p=ack_wire(a);z=ack_end(i,a)
        if z['UNA']!=a['ACK']:raise ValueError('cumulative ACK did not advance sender to acknowledged sequence')
        return {**point_row(a,t0,seq),'ack':(a['ACK']-seq)%mod,'sacks':[[(b['Start']-seq)%mod,(b['End']-seq)%mod] for b in (a['SACK'] or [])], 'new_sack':z['NewSACK'],'wire_sha256':p['Hash']}
    sparse_w=[p for p in rev if sparse(p['ACK'],p['SACK'])]; epochs=[]
    for start,r in enumerate(states):
        if r['Kind']!='rto_interrupt' or not r['Active'] or r['State']!=SACK_RECOVERY:continue
        point=(r['NXT']-1)%mod; ep={'timeout':point_row(r,t0,seq),'point':(point-seq)%mod,'partial':[],'cover':None,'entries':[],'loss_callbacks':[],'preceding_sparse_sacks':sum(p['Due']<=r['At'] for p in sparse_w)}
        for i in range(start+1,len(states)):
            e=states[i]
            if e['Kind']=='ack_begin' and ahead(e['ACK'],e['UNA']) and not ahead(e['ACK'],e['NXT']):
                wire=evidence(i,e)
                if ahead(e['ACK'],point): ep['cover']=wire;break
                ep['partial'].append(wire)
            if e['Kind']=='enter_recovery' and not ahead(e['UNA'],point):ep['entries'].append(point_row(e,t0,seq))
            if e['Kind']==loss+'begin' and not ahead(e['UNA'],point):
                (_,a,b)=next(p for p in losses if p[0]==i); ep['loss_callbacks'].append(cb(a,b))
        epochs.append(ep)
    if schedule=='none' and (not complete or s['RTOs'] or s['Retransmissions'] or s['Drops'] or losses or epochs): raise ValueError('lossless calibration failed')
    return {'algorithm':algorithm,'path':schedule,'completed':complete,'elapsed_ns':elapsed,'delivered':s['Delivered'],'acked':s['Acknowledged'],'accepted':s['Accepted'],'eof':s['EOF'],'fin_ack':s['FinACK'],'pending':s['Pending'],'body_sha256':s['BodyHash'], 'rtos':s['RTOs'],'retransmissions':s['Retransmissions'],'sack_recoveries':s['SACKRecoveries'],'forward_drops':s['Drops'],'receiver_sacks':len(sparse_w), 'controller_losses':[cb(a,b) for _,a,b in losses],'controller_timeouts':[cb(a,b) for _,a,b in timeouts],'epochs':epochs,'enrolled':any(ep['preceding_sparse_sacks'] and any(p['sacks'] and p['new_sack'] and p['state']==RTO_RECOVERY for p in ep['partial']) for ep in epochs),'wire_sha256':digest(w),'events_sha256':digest(ev)}


def measure(samples):
    rows={}
    for alg in ('reno','cubic'):
        for path in ('none','interrupted_sack'):
            name=alg+'_'+path; rows[name]=summarize(samples[ROOT+'/'+name],alg,path)
    if len(samples)!=len(rows):raise ValueError('Unexpected native case')
    return rows


def compare(before,after):
    comparisons={}
    for name in before:
        a,b=before[name]['Events'],after[name]['Events']
        def first_diff(u,v):
            i=next((i for i,(x,y) in enumerate(zip(u,v)) if x!=y),min(len(u),len(v)))
            if len(u)==len(v)==i:return None
            return {'index':i,'before':u[i] if i<len(u) else None,'after':v[i] if i<len(v) else None}
        delta=first_diff(a,b)
        w_a=[e['Wire'] for e in a if 'Wire' in e];w_b=[e['Wire'] for e in b if 'Wire' in e]
        interrupted=[i for i,x in enumerate(a) if (r:=x.get('Sender')) and r['Kind']=='rto_interrupt' and r['Active'] and r['State']==SACK_RECOVERY]
        if name.endswith('_none') and delta is not None:raise ValueError('No-loss source controls differed')
        if delta is not None and (not interrupted or delta['index']<=interrupted[0]):raise ValueError('Comparison diverged before the interrupted recovery could act')
        comparisons[name]={'identical_event_prefix':delta['index'] if delta else len(a),'first_interrupted_event':interrupted[0] if interrupted else None,'first_event_difference':delta,'first_wire_difference':first_diff(w_a,w_b),'equal_received_prefix':before[name]['BodyHash']==after[name]['BodyHash'], 'both_complete':all(x[name]['EOF'] and x[name]['FinACK'] and not x[name]['Pending'] and x[name]['Acknowledged']==len(BODY) and x[name]['Delivered']==len(BODY) for x in (before,after))}
    return comparisons


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
    child, stock, base = (config[k] for k in ("source", "stock", "product_base"))
    if (config["targets"] != [OWNER] or config["expected_sources"] != [child, stock]
            or subprocess.check_output(["git", "rev-parse", child + "^", child + "^^"], text=True).splitlines() != [stock, base]):
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
        for label, rev in (("stock", stock), ("boundary", child)):
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
        result["comparisons"] = compare(full_samples["stock"], full_samples["boundary"])
        result["status"] = "PASS"
    finally:
        subprocess.run(["git", "switch", "--detach", child], check=True)
        (evidence / "cubic-reno-interrupted.json").write_text(json.dumps(result, indent=2) + "\n")
