"""Stage immutable guest inputs and grade the two actual local Bazel owners."""
import ast
import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import sys
import zipfile

ROOT = Path(sys.argv[1]).resolve()
OUT = ROOT / "result.json"
BASE = Path("test/rbe/tcg")
OBS = "55389ccd4ddad77ee81531a7acf0d552d57471dd"
VM = "64fea59014b0104240a5e100095b60e51aa90bae"
CASES = ("stock", "fixed")
ORDER = ("client-first", "server-first")
ELFS = {
    "stock": (13359584720, "04a7314cb8d1efacf59a696a68755eeb1ffca8afca948f7af809cbee2a10dc0c",
              103661048, "d6c236e3dd43a203f28f184ff6545e76d2b28db30f78966864c6448840456a98", "9f3e9a3fba9416ea1f5832d82923d95830479096"),
    "fixed": (13360118839, "1b32c287347364392ce7c8990536f0cdc30516138dddf962de42d6c33e730bb4",
              103665416, "f16fd6690a2959d866206816b9ca00c6f1579aabd2a6f5358a599ed6017215c6", "9ffcd1079387ea27430c721bba4014782257a6f1"),
}
SOURCES = ("dind_guest.sh", "dind_socket.py", "nested_docker.py")
TAGS = ("DIND_STEP", "DIND_MTU", "DIND_FORWARD", "DIND_END", "DIND_DAEMON",
        "DIND_NETWORK", "DIND_SERVER", "DIND_SNAT", "DIND_CT", "DIND_ROUTE")
LABEL = "//test/rbe/tcg:"
IMAGE = "amd64_cgroup_v1_guest"
TOOLS = "kvm_host_tools"


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def save(r):
    temp = OUT.with_suffix(".new")
    temp.write_text(json.dumps(r, sort_keys=True))
    temp.replace(OUT)


def original(path):
    return subprocess.check_output(["git", "show", OBS + ":" + path])


def helper(name):
    # Copy only two known pure functions from the immutable original module,
    # not its top-level Bazel result collector.
    src = original(".github/qualification/nested_docker_receipt.py")
    node, = [n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == name]
    space = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "original_flow_reducer", "exec"), space)
    return space[name]


def stage(r):
    inputs = ROOT / "inputs"
    qa = inputs / "qa"
    qa.mkdir(parents=True)
    ips = inputs / "iproute2"
    ips.mkdir()
    ip = Path(shutil.which("ip", path="/usr/sbin:/usr/bin:/sbin:/bin")).resolve(strict=True)
    assert ip.is_relative_to("/usr")
    libs = [Path(p) for p in subprocess.check_output(["ldd", ip], text=True).split() if p.startswith("/")]
    assert any(p.name == "ld-linux-x86-64.so.2" for p in libs)
    for p in (ip, *libs):
        assert p.resolve(strict=True).is_relative_to("/usr")
        shutil.copy2(p.resolve(strict=True), ips / p.name)
    r["iproute2"] = dict(version=subprocess.check_output(["dpkg-query","-W","-f=${Version}","iproute2"],text=True),
                          sha256=sha(ips/"ip"))
    digests = {}
    for name in SOURCES:
        content = original("test/rtnetlink/linux/" + name)
        digests[name] = hashlib.sha256(content).hexdigest()
        if name != "nested_docker.py":
            (qa / name).write_bytes(content)
    r["fixture_sha256"] = digests
    source = ast.parse(content)
    value = lambda key: next(x.value for x in source.body if isinstance(x,ast.Assign)
                             and isinstance(x.targets[0],ast.Name) and x.targets[0].id == key)
    (inputs / "host-setup.sh").write_text(ast.literal_eval(value("HOST_SETUP")))
    # Source-bound construction: use the original mount and capability code,
    # changing only the two paths which cross this guest's read-only disk.
    f, = [x for x in source.body if isinstance(x,ast.FunctionDef) and x.name == "main"]
    statements = next(x for x in f.body if isinstance(x,ast.Try)).body
    mark = lambda n: isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name)
    begin, = [i for i,n in enumerate(statements) if mark(n) and n.targets[0].id == "mounts"]
    end, = [i for i,n in enumerate(statements) if mark(n) and n.targets[0].id == "spec"]
    scope = dict(qa="/input/qa",iptool="/input/iproute2")
    exec(compile(ast.Module(body=[next(x for x in source.body if isinstance(x,ast.Assign) and x.value is value("CAPS")),
                                     *statements[begin:end+1]],type_ignores=[]),"original_oci_config","exec"),scope)
    spec = scope["spec"]
    assert [(m["source"],m["destination"]) for m in spec["mounts"] if m["type"] == "bind"] == [("/input/qa","/qa"),("/input/iproute2","/iproute2")]
    (inputs / "config.json").write_text(json.dumps(spec))
    root_tar = ROOT/"dind-rootfs.tar"
    with tarfile.open(root_tar, "r|") as t:
        unpacked = sum((m.size+4095)//4096*4096 if m.isfile() else 4096 for m in t)
    r["rootfs_staging"] = dict(archive_bytes=root_tar.stat().st_size, estimated_unpacked_bytes=unpacked,
                               fixture_tmpfs_bytes=2*1024**3)
    save(r)
    assert 0 < unpacked <= 2*1024**3 - 256*1024**2
    shutil.move(root_tar,inputs/"dind-rootfs.tar")
    shutil.move(ROOT/"python-image.tar",qa/"python-image.tar")
    for arm,(aid,archive,n,digest,source) in ELFS.items():
        zpath = ROOT / (arm + ".zip")
        assert sha(zpath) == archive
        path = inputs / arm
        path.mkdir()
        with zipfile.ZipFile(zpath) as z:
            member, = z.infolist()
            assert member.filename == arm+"-runsc" and member.file_size == n
            with z.open(member) as reader, (path/"runsc").open("wb") as writer:
                shutil.copyfileobj(reader,writer)
        assert sha(path/"runsc") == digest
        (path/"runsc").chmod(0o755)
        shutil.copytree(ROOT/"july/usr/bin/gvisor-bin",path/"gvisor-bin")
        r["binaries"][arm] = dict(source=source,artifact=aid,sha256=digest,bytes=n,archive_sha256=archive)
        zpath.unlink()
    (inputs/"fixture.sh").write_text("""set -eu
arm=$1
test "$(id -u)" = 0
for s in bash unshare mount timeout cp mkdir; do command -v "$s" >/dev/null; done
command -v tar >/dev/null || ln -s /bin/busybox /usr/bin/tar
printf 'ECHO_VM_ELF %s %s\\n' "$arm" "$(sha256sum "/input/$arm/runsc" | cut -c1-64)"
mkdir -p /tmp/dind
echo "ECHO_VM_STAGE nested-runtime"
rc=0
timeout --kill-after=10 675 unshare --mount --net --propagation private \\
  /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C \\
  /bin/bash /input/host-setup.sh /tmp/dind /input/dind-rootfs.tar /input/config.json "/input/$arm/runsc" || rc=$?
printf 'ECHO_VM_RUNTIME %s %s\\n' "$arm" "$rc"
exit "$rc"
""")
    used = sum(p.stat().st_size for p in inputs.rglob("*") if p.is_file())
    assert 250*1024**2 < used < 2400*1024**2
    disk = BASE / "july_bridge_inputs.ext4"
    subprocess.run(["truncate", "-s", str(used+300*1024**2), disk],check=True)
    subprocess.run(["/usr/sbin/mke2fs","-q","-F","-t","ext4","-d",inputs,disk],check=True)
    r["input_disk_sha256"] = sha(disk)
    r["stage"] = "READY"
    save(r)


def socket_cases(lines, arm, row):
    """Preserve measured errors, accepted sockets, incomplete captures and actual ISNs."""
    for tag in TAGS:
        row[tag] = [s[len(tag)+1:] for s in lines if s.startswith(tag+" ")][:18]
    row["backend"] = next((s.split()[1] for s in lines if s in ("ECHO_VM_BACKEND kvm","ECHO_VM_BACKEND tcg")),None)
    row["guest_ready"] = f"ECHO_VM_READY {arm} 6.8.0-138-generic" in lines
    row["guest_elf_match"] = f"ECHO_VM_ELF {arm} {ELFS[arm][3]}" in lines
    row["guest_runtime_exit"] = [int(m[1]) for s in lines if (m:=re.fullmatch("ECHO_VM_RUNTIME "+arm+r" (\d{1,3})",s))]
    row["guest_end"] = f"ECHO_VM_END {arm} 0" in lines
    row["qemu_exit"] = [int(m[1]) for s in lines if (m:=re.fullmatch(r"ECHO_VM_EXIT (\d{1,3})",s))]
    row["tallies"] = [json.loads(s[6:]) for s in lines if s.startswith("TALLY ")]
    row["cells"] = [json.loads(s[5:]) for s in lines if s.startswith("CELL ")]
    row["socket_attempts_reported"] = sum(c["attempted"] for c in row["tallies"])
    row["witnesses"] = {}
    for c in row["cells"]:
        mode = c["mode"]
        assert mode in ORDER
        flow = c["flow"]
        verdict = {}
        row["witnesses"][mode] = verdict
        for name in ("success","first","first_bound_reset"):
            key = flow["endpoints"].get(name)
            if not key:
                continue
            assert ipaddress.ip_address(key[0]).is_private and 0 < key[1] < 65536
            client = flow["client"].get(name)
            server = (flow["server"] or {}).get(name)
            witness = dict(endpoint=key, ordinals=flow["indices"].get(name), observer={},
                           accepted_reports=server.get("accepted") if server else None)
            verdict[name] = witness
            for side,view in (("client",client),("server",server)):
                if not view:
                    continue
                headers = view["headers"]
                assert len(headers) <= 80 and all(len(p) == 7 and p[0] in ("S","C")
                                                  and all(type(i) is int for i in p[1:]) for p in headers)
                syn = sorted({p[3] for p in headers if p[0] == "C" and p[2]&0x17 == 2})
                # Count distinct server sequence numbers for this same client SYN;
                # repeated frames (including mirror copies) have only one ISN.
                isn = sorted({p[3] for p in headers if p[0] == "S" and p[2]&0x17 == 18
                              and (p[4]-1)%2**32 in syn})
                witness["observer"][side] = dict(client_syn=syn,server_isn=isn,
                    reset_headers=[p for p in headers if p[2]&4],truncated=view["truncated"],errno=view["observer_errno"])
            if name == "success":
                witness["confirmed"] = bool(client and server and
                    all(not v["truncated"] and v["observer_errno"] is None and helper("success_control")(v)
                        for v in (client,server)) and any(a["received"] == 1 and a["sent"] for a in server["accepted"]))
        verdict["original_reset_classification"] = helper("first_reset")(c)
        verdict["first_capture_complete"] = bool(c.get("first") and
            set(verdict.get("first",{}).get("observer",{})) == {"client","server"} and
            all(not s["truncated"] and s["errno"] is None for s in verdict["first"]["observer"].values()))
    assert row["guest_ready"] and row["guest_elf_match"] and row["guest_end"] and row["backend"]
    assert row["qemu_exit"] == row["guest_runtime_exit"] == [0]
    assert row["DIND_END"] == ["finished 0"] and row["DIND_STEP"][-1:] == ["finished"]
    assert row["DIND_MTU"][-1].split()[1] == "1460" and row["DIND_FORWARD"] == ["1"]
    assert row["DIND_DAEMON"][0].split()[0] == "27.5.1" and "dev eth0" in row["DIND_ROUTE"][0]
    assert [x.split()[0] for x in row["DIND_SNAT"]] == ["tcp","udp"]
    assert [c["mode"] for c in row["tallies"]] == [c["mode"] for c in row["cells"]] == list(ORDER)
    servers = dict(s.split() for s in row["DIND_SERVER"])
    for t,c in zip(row["tallies"],row["cells"]):
        assert all(c[k]==v for k,v in t.items())
        errors = sum(e["count"] for e in c["errors"])
        assert c["attempted"] == c["completed"]+errors == 2000 and (c["first"] is None) == (errors==0)
        assert c["completed"] <= c["echoes"] <= 2000
        assert (c["eofs"]==0 if c["mode"]=="client-first" else c["completed"]<=c["eofs"]<=c["echoes"])
        assert c["dns"] == ["127.0.0.11"] and not c["static_srv"] and c["srv_ips"] == [servers[c["mode"]]]
        assert c["python"].startswith("3.12.") and row["witnesses"][c["mode"]]["success"]["confirmed"]
    row["status"] = ("OBSERVED_SOCKET_ERRORS" if any(c["first"] for c in row["cells"]) else "NO_SOCKET_ERRORS")


def collect(r):
    r["setup"] = (ROOT/"setup").read_text().split() if (ROOT/"setup").exists() else None
    if not (ROOT/"exit").exists():
        save(r)
        return
    rc = int((ROOT/"exit").read_text())
    r["bazel_exit"] = rc
    save(r)
    # Reuse the qualified July build reporter's bounded diagnostics only.
    tree = ast.parse((ROOT/"build-report.py").read_text())
    names = ("FORBIDDEN","DIAGNOSTIC")
    wanted = [n for n in tree.body if (isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name) and n.targets[0].id in names)
              or (isinstance(n,ast.FunctionDef) and n.name=="diagnostics")]
    space = {"re":re,"Path":Path,"ROOT":ROOT}
    exec(compile(ast.Module(body=wanted,type_ignores=[]),"original_build_diagnostics","exec"),space)
    diag = space["diagnostics"]
    events = ROOT/"events"
    completed = {}
    attempts = {a:[] for a in CASES}
    failed = []
    if events.exists():
        with events.open() as data:
            for line in data:
                e = json.loads(line); ident=e.get("id",{})
                if "targetCompleted" in ident:
                    owner=ident["targetCompleted"]["label"]
                    if owner.startswith(LABEL):
                        completed[owner] = bool(e.get("completed",{}).get("success"))
                if "action" in e and not e["action"].get("success") and len(failed)<3:
                    a=e["action"]; ref=ident.get("actionCompleted",{})
                    failed.append({k:v for k,v in {"owner":ref.get("label"),"mnemonic":a.get("type"),"exit":a.get("exitCode")}.items()
                                   if type(v) is int or (isinstance(v,str) and re.fullmatch(r"[\w@.+/:-]{1,220}",v))})
                if "testResult" in ident and "testResult" in e:
                    for arm in CASES:
                        if ident["testResult"]["label"] != LABEL+"july_bridge_"+arm:
                            continue
                        tr=e["testResult"]; ex=tr.get("executionInfo",{})
                        attempts[arm].append(dict(status=tr["status"],runner=ex.get("strategy"),
                            cached=bool(tr.get("cachedLocally") or ex.get("cachedRemotely")),start=tr.get("testAttemptStart"),
                            duration=tr.get("testAttemptDuration"),original_log=any(f.get("name")=="test.log" for f in tr.get("testActionOutput",[]))))
    r.update(failed_actions=failed,completed=completed)
    image_dir = Path("bazel-bin/test/rbe/tcg")
    if completed.get(LABEL+IMAGE) and completed.get(LABEL+TOOLS):
        r["vm_outputs"] = {}
        for name, path in (("kernel",image_dir/(IMAGE+".Image")),("rootfs",image_dir/(IMAGE+".ext4")),
                           ("emulator",image_dir/TOOLS/"usr/bin/qemu-system-x86_64"),
                           ("firmware",image_dir/TOOLS/"usr/share/seabios/bios-256k.bin")):
            if path.is_file():
                r["vm_outputs"][name] = dict(bytes=path.stat().st_size,sha256=sha(path))
    if rc and (ROOT/"build.log").exists():
        with (ROOT/"build.log").open("rb") as f:
            r["build_diagnostics"] = diag(f.read(600000).decode(errors="replace"))
    for arm in CASES:
        owner = LABEL+"july_bridge_"+arm
        row=r["arms"][arm]=dict(owner=owner,status="NO_TEST_RESULT",attempts=attempts[arm])
        path=Path("bazel-testlogs/test/rbe/tcg", "july_bridge_"+arm,"test.log")
        if not attempts[arm] or not path.is_file() or path.stat().st_size > 4*1024**2:
            continue
        raw=path.read_bytes()
        row["test_log_sha256"]=hashlib.sha256(raw).hexdigest()
        try:
            lines=raw.decode(errors="replace").splitlines()
            socket_cases(lines,arm,row)
            a,=row["attempts"]
            assert a["status"]=="PASSED" and a["runner"]=="local" and not a["cached"] and a["original_log"]
        except (AssertionError,IndexError,KeyError,TypeError,ValueError) as exc:
            row.update(status="INCOMPLETE",review_error=type(exc).__name__)
        save(r)
    if rc==0 and all(r["arms"][a]["status"] in ("NO_SOCKET_ERRORS","OBSERVED_SOCKET_ERRORS") for a in CASES):
        assert set(r.get("vm_outputs",{}))=={"kernel","rootfs","emulator","firmware"}
        assert r["arms"]["stock"]["backend"]==r["arms"]["fixed"]["backend"]
        assert r["stage"]=="READY" and r["setup"]==["ready","0"]
        r["status"]="MEASURED"
    r["verified_at"]=now()
    save(r)


record = (json.loads(OUT.read_text()) if OUT.exists() else dict(status="INCOMPLETE",binaries={},arms={},
                      vm_source=VM,fixture=OBS,original_packet_artifact=13360000336,build_artifact=13360069037))
try:
    if sys.argv[2]=="stage":
        stage(record)
    else:
        collect(record)
except Exception as exc:
    record["report_error"]=type(exc).__name__
    record["error_line"]=exc.__traceback__.tb_lineno
    raise
finally:
    save(record)
