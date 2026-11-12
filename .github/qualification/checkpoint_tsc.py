"""Run the original KVM clock test on the published source and two explicit sampling boundaries."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from xml.etree import ElementTree as ET

import native
import previous

BASE = "2f5253abfaa87b6d62d79a0da60942e773389934"
PRODUCT = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
assert subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip() == BASE
TARGET = "//runsc/container:container_test"
TEST = "TestCheckpointRestoreAdjustedTSC"
HOST = Path("runsc/container/container_save_restore_test.go")
GUEST = Path("test/cmd/test_app/vdso_clock_unsafe.go")
OUT = native.OUT
expected_fail = "RDTSC went backwards across first restore from host with higher TSC"
report = {"product": PRODUCT, "base": BASE, "arms": {}, "device": {}}
previous.OUT = native.WORK
previous.REPORT["assets"] = report.setdefault("inputs", {})
native.GO[TARGET] = [TEST, TEST + "/kvm"]

def edit(path, old, new):
    data = path.read_text()
    assert data.count(old) == 1, old
    path.write_text(data.replace(old, new))

def gate():
    edit(GUEST, "\t\tmsg := fmt.Sprintf(\"OK: vdso=", '''		if iter == 2 || iter == 4 {
            ready := fmt.Sprintf("%s.ready%d", v.file, iter)
            release := fmt.Sprintf("%s.release%d", v.file, iter)
            if err := os.WriteFile(ready, []byte(fmt.Sprint(curTSC)), 0666); err != nil { log.Fatal(err) }
            for {
                if _, err := os.Stat(release); err == nil { break } else if !os.IsNotExist(err) { log.Fatal(err) }
                time.Sleep(5 * time.Millisecond)
            }
        }
        msg := fmt.Sprintf("OK: vdso=''')
    edit(HOST, '\t// Checkpoint running container (first save).', '''    waitForSample := func(iter int) {
            t.Helper()
            name := fmt.Sprintf("%s.ready%d", outputPath, iter)
            if err := testutil.Poll(func() error {
                v, err := os.ReadFile(name)
                if err != nil { return err }
                if _, err := strconv.ParseUint(string(v), 10, 64); err != nil { return err }
                t.Logf("sampled before checkpoint: iteration=%d tsc=%s", iter, v)
                return nil
            }, 10*time.Second); err != nil { t.Fatal(err) }
        }
        releaseSample := func(iter int) {
            t.Helper()
            if err := os.WriteFile(fmt.Sprintf("%s.release%d", outputPath, iter), nil, 0666); err != nil { t.Fatal(err) }
            t.Logf("released after restore: iteration=%d", iter)
        }
        waitForSample(2)
        // Checkpoint running container (first save).''')
    for wait_at, code in [
        ('\t// Checkpoint the second container (second save)', 'waitForSample(4)'),
        ('\t// Wait for third container to finish executing', 'releaseSample(4)'),
    ]:
        edit(HOST, wait_at, '\t'+code+'\n'+wait_at)
    needle='t.Fatalf("error restoring second container: %v", err)\n\t}\n'
    edit(HOST,needle,needle+'\treleaseSample(2)\n')

def run_arm(name, source, instrument, missing_offset):
    sub = OUT / name
    sub.mkdir()
    subprocess.run(["git","checkout","-q","--detach",source],check=True)
    assert not subprocess.check_output(["git","status","--short","--untracked-files=no"])
    if instrument: gate()
    if missing_offset:
        edit(HOST, 'strconv.FormatUint(cycle+offsetCycles, 10)', 'strconv.FormatUint(cycle, 10)')
    (sub/"source.diff").write_bytes(subprocess.check_output(["git","diff","--",str(HOST),str(GUEST)]))
    native.OUT=sub
    native.REPORT={"commands":{},"tests":{}}
    flags=["--test_filter=^"+TEST+"$/^kvm$", "--test_env=TEST_PLATFORMS=kvm",
           "--test_env=GO_TEST_WRAP=1","--test_env=GO_TEST_WRAP_TESTV=1","--test_arg=-test.v"]
    native.run(name,[TARGET],flags,55)
    r=native.REPORT["tests"][TARGET]
    assert r["strategy"] in ("local","standalone") and not r["cached"] and r["attempt"]==r["shard"]==1
    for key in ["test.log","test.xml"]:
        assert hashlib.sha256((sub/r[key]["file"]).read_bytes()).hexdigest()==r[key]["sha256"]
    log=(sub/r["test.log"]["file"]).read_text()
    xml=ET.parse(sub/r["test.xml"]["file"])
    assert "=== RUN   "+TEST+"/kvm" in log, log[-1800:]
    assert not list(xml.iter("skipped")) and not re.search(r"--- SKIP: "+TEST,log)
    expected = source==BASE or missing_offset
    assert (r["status"]=="FAILED" if expected else r["status"]=="PASSED"),(r["status"],r.get("observation_error"))
    result_rows=re.findall(r"OK: vdso=(\d+) sys=(\d+) diff=(-?\d+) tsc=(\d+)",log)
    # The standard verbose runner logs all thirty rows via t.Logf.
    assert len(result_rows)>=30, len(result_rows)
    rows=[int(row[3]) for row in result_rows[:30]]
    assert all(a<=b for a,b in zip(rows,rows[1:])),rows
    if instrument:
        for it in (2,4):
            m=re.search(r"sampled before checkpoint: iteration="+str(it)+r" tsc=(\d+)",log)
            assert m and int(m.group(1))==rows[it-1],(it,rows)
            assert f"released after restore: iteration={it}" in log
    if expected:
        assert expected_fail in log and '--- FAIL: '+TEST+'/kvm' in log
        assert list(xml.iter("failure")) or list(xml.iter("error"))
    else: assert '--- PASS: '+TEST+'/kvm' in log and not list(xml.iter("failure"))
    report["arms"][name]={"source":source,"verified":True,"status":r["status"],"cycles":rows[:6],"controls":instrument,
        "missingOffset":missing_offset,"test.log":r["test.log"]["sha256"],"test.xml":r["test.xml"]["sha256"]}
    (OUT/"summary.json").write_text(json.dumps(report,indent=2)+"\n")

import fcntl
fd=os.open("/dev/kvm",os.O_RDWR|os.O_CLOEXEC)
report["device"]["api"]=fcntl.ioctl(fd,0xAE00)
vm=fcntl.ioctl(fd,0xAE01,0)
os.close(vm); os.close(fd)
assert report["device"]["api"]==12
for arm in [("stock-gated",BASE,True,False),("revised-gated",PRODUCT,True,False),
            ("revised-ordinary",PRODUCT,False,False),("missing-offset",PRODUCT,True,True)]:
    try: run_arm(*arm)
    finally: subprocess.run(["git","restore","--",str(HOST),str(GUEST)],check=True)
assert len(report["arms"])==4
