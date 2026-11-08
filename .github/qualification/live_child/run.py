"""Run the original complete-checkpoint fixture with the two current encoder builds."""
import ast
import hashlib
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import shutil
import xml.etree.ElementTree as ET
import struct
import subprocess
import sys
from urllib.parse import unquote, urlsplit
CAPSULE_SOURCE = "8d980b39ad21950f42c5aeec48935d52c3e50b85"
BUSYBOX = "dbac288c29ba568459550a2da9e7ae0ded6b1fc728ee9fad3044c44e62d6ac14"

BASE = "7d1b049f5d541992b47cc38cf564025908a9ed7b"
FIX = "2910f8cc0188709d442b3e1865ab03805e8d9d1a"
BASE_TREE = "67a49c9690c1a2437652ab4eb723d4fd847d430e"
FIX_TREE = "6ab521fe268a9ad59e9cb4d74b65dc03f0042b45"
FIXTURE = "d280d49cf180e6443fdba49d57260c3bfd471ff5"
OLD_BASE = "aa767e4c43d011121271551fe24da501a56f729d"
VM = "631e08f3b1687c026e09566d0f3d61654f5e1f1e"
OWNER = "//runsc/container:container_test"
PACKAGE = Path(".github/qualification/restore-limit-vm")
NATIVE = "//.github/qualification/live_child:paired"
TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "results"
COMMON = ["--jobs=4", "--lockfile_mode=off", "--remote_executor=", "--remote_cache=", "--bes_backend=",
          "--spawn_strategy=processwrapper-sandbox,local", "--strategy=GoStdlib=processwrapper-sandbox",
          "--strategy=GoStdlibList=processwrapper-sandbox"]
STAGE = sys.argv[1]
row = {"baseline": BASE, "candidate": FIX, "fixture": FIXTURE, "original_fixture_runner": VM, "capsule_source": CAPSULE_SOURCE}

def save():
    (OUT / (STAGE + ".json")).write_text(json.dumps(row, indent=2, sort_keys=True) + "\n")

def sha(path):
    with path.open("rb") as f:
        h = hashlib.sha256()
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()

def git(*args):
    return subprocess.check_output(["git", *args])

def checkout(revision):
    git("checkout", "-q", "--detach", revision)
    assert git("rev-parse", "HEAD").decode().strip() == revision
    git("diff", "--quiet", "HEAD")

def run(name, cmd, **kw):
    log = TMP / (name + ".log")
    with log.open("wb") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, **kw)
    if result.returncode:
        lines = log.read_text(errors="replace").splitlines()
        row["failed_command"] = {"name": name, "exit": result.returncode,
            "errors": [s[:450] for s in lines if re.search(r"(?i)error:|failed|no space|operation not permitted|cannot", s)][-9:],
            "tail": [s[:300] for s in lines[-6:]]}
        save()
        raise RuntimeError((name, result.returncode))
    return log

def bazel(version, name, command, *args):
    startup = [str(TMP / ("bazel" + version)), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g"]
    return run(name, [*startup, command, *args])

def locate(name, target):
    lines = bazel("9", name, "cquery", *COMMON, "--config=x86_64", "-c", "opt", "--output=files", target).read_text().splitlines()
    paths = [Path(s) for s in lines if s.startswith("bazel-out/") and Path(s).is_file() and os.access(s, os.X_OK)]
    path, = paths
    return path

def find_test_app(top):
    # Match testutil.FindFile's direct path, then its single wildcard component.
    for pattern in ("test/cmd/test_app/test_app", "test/cmd/test_app/*/test_app"):
        paths = list(top.glob(pattern))
        if paths:
            path, = paths
            assert path.is_file(), path
            return path
    raise FileNotFoundError(str(top / "test/cmd/test_app"))

def build():
    assert git("rev-parse", "HEAD^").decode().strip() == FIX and git("rev-parse", "HEAD^^").decode().strip() == BASE
    row["source_trees"] = {arm: git("rev-parse", source+"^{tree}").decode().strip() for arm, source in (("stock", BASE), ("fixed", FIX))}
    assert row["source_trees"] == {"stock": BASE_TREE, "fixed": FIX_TREE}, row["source_trees"]
    fixture_file = "runsc/container/container_save_restore_test.go"
    assert git("rev-parse", FIXTURE+":"+fixture_file).decode().strip() == "6818655c7dd2c3a8b9b0557ba12112f1a18b5c73"
    patch = git("diff", "--binary", OLD_BASE, FIXTURE, "--", "runsc/container")
    original = git("show", FIXTURE+":"+fixture_file).split(b"\nfunc TestCheckpointTmpfsTreeForProfile(")[1]
    row["fixture_function_sha256"] = hashlib.sha256(original).hexdigest()
    row["arms"] = {}
    for arm, source in [("stock", BASE), ("fixed", FIX)]:
        checkout(source)
        subprocess.run(["git", "apply", "-"], input=patch, check=True)
        try:
            assert Path(fixture_file).read_bytes().count(b"func TestCheckpointTmpfsTreeForProfile(" + original) == 1
            # Keep the original Checkpoint call, graph checks, Restore and 50k
            # verification. The user-mapped host has no delegated memory cgroup.
            file = Path(fixture_file)
            prefix, func = file.read_bytes().split(b"func TestCheckpointTmpfsTreeForProfile(", 1)
            def replace(old, new):
                nonlocal func
                assert func.count(old) == 1, old
                func = func.replace(old, new)
            replace(b"\tconst limit int64 = 2 << 30\n\tmemLimit := limit\n", b"")
            replace(b'\tcgPath := "/" + testutil.RandomID("gvisor-profile-cg")', b'\tconf.Rootless, conf.IgnoreCgroups = true, true\n\tspec.Root.Path = "/guest-rootfs"')
            start, stop = func.index(b"\tspec.Linux = &specs.Linux{"), func.index(b"\t_, bundle, cleanup, err :=")
            func = func[:start] + b"\tspec.Linux = &specs.Linux{}\n" + func[stop:]
            start, stop = func.index(b"\tcheckCap := func("), func.index(b"\tcont, err := New(")
            func = func[:start] + func[stop:]
            for stage in (b"save", b"restore"):
                replace(b'\tcheckCap("' + stage + b'")\n', b"")
            assert func[func.index(b'\twork(cont, "fsTreeCreate")'):].replace(b'\trpc := time.Now()', b'\tcheckCap("restore")\n\trpc := time.Now()') == original[original.index(b'\twork(cont, "fsTreeCreate")'):]
            file.write_bytes(prefix + b"func TestCheckpointTmpfsTreeForProfile(" + func)
            bazel("9", "build-" + arm, "build", *COMMON, "--config=x86_64", "-c", "opt", OWNER)
            owner = locate("locate-" + arm, OWNER)
            assert owner.name == "container_test" and Path(str(owner)+".runfiles/_main").is_dir()
            root = Path(str(owner) + ".runfiles/_main")
            files = {"owner": owner, "runsc": root/"release/runsc", "sentry": root/"release/gvisor-bin/gvisor_sentry",
                     "test_app": find_test_app(root)}
            for p in files.values():
                with p.open("rb") as f: h = f.read(20)
                assert h[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H", h, 18)[0] == 62, str(p)
            arm_dir = TMP / "live-build" / arm
            arm_dir.mkdir(parents=True)
            rec = row["arms"][arm] = {k: sha(p) for k, p in files.items()}
            rec.update(source=source, fixture_blob=git("hash-object", fixture_file).decode().strip(), owner_path=str(owner))
            (arm_dir / "elf.sha256").write_text(rec["owner"]+"\n")
            tar = arm_dir / "capsule.tar"
            run("archive-"+arm, ["tar", "--hard-dereference", "-chf", str(tar), "container_test", "container_test.runfiles"], cwd=owner.parent)
            assert 0 < tar.stat().st_size <= (1 << 30), tar.stat().st_size
            rec.update(capsule=sha(tar), capsule_bytes=tar.stat().st_size)
            save()
        finally:
            git("restore", "--", "runsc/container/BUILD", fixture_file)
    row["original_artifact"] = 13600989948
    row["shared_test_app"] = row["arms"]["stock"]["test_app"]
    row["previous_stock_test_app"] = "678908205576c6beafcf04cf8106df35530d6e84efb37a31fa1be75c626916a8"
    row["stock_test_app_reproduced"] = row["shared_test_app"] == row["previous_stock_test_app"]
    assert row["arms"]["stock"]["sentry"] != row["arms"]["fixed"]["sentry"]
    assert row["arms"]["stock"]["fixture_blob"] == row["arms"]["fixed"]["fixture_blob"]
    module = Path("MODULE.bazel")
    orig = module.read_bytes()
    try:
        addition = (TMP / "benchstat.module").read_bytes()
        assert hashlib.sha256(addition).hexdigest() == "373bba0a70f04c2a39a30a45b4acd23f3bfa884e05a5e27cce1ce7010c406cd5"
        module.write_bytes(orig + b"\n" + addition)
        stats = "@org_golang_x_perf//cmd/benchstat:benchstat"
        bazel("9", "benchstat-build", "build", *COMMON, "--config=x86_64", "-c", "opt", stats)
        shutil.copy2(locate("locate-stats", stats), TMP/"benchstat")
        row["benchstat_sha256"] = sha(TMP/"benchstat")
    finally:
        module.write_bytes(orig)
    row["verified"] = True


def host_info():
    assert os.getuid() == os.geteuid() != 0
    status = Path("/proc/self/status").read_text()
    assert int(re.search(r"(?m)^CapEff:\s*(\w+)$", status)[1], 16) == 0
    chosen = sorted(os.sched_getaffinity(0))[:2]
    assert len(chosen) == 2
    limits = []
    entries = Path("/proc/self/cgroup").read_text().splitlines()
    v2 = next((s.split(":",2)[2] for s in entries if s.startswith("0::")), None)
    if v2 is not None and Path("/sys/fs/cgroup/cgroup.controllers").exists():
        root, leaf, filename = Path("/sys/fs/cgroup"), v2, "memory.max"
        mode = "cgroup2"
    else:
        root = Path("/sys/fs/cgroup/memory")
        leaf, = [s.split(":",2)[2] for s in entries if "memory" in s.split(":")[1].split(",")]
        filename, mode = "memory.limit_in_bytes", "cgroup1"
    path = (root / leaf.lstrip("/")).resolve()
    assert path.is_relative_to(root)
    while True:
        item = path / filename
        if item.is_file():
            value = item.read_text().strip()
            assert value == "max" or value.isdigit()
            limits.append(value)
        if path == root:
            break
        path = path.parent
    # The cgroup v2 hierarchy root may itself legitimately omit memory.max.
    assert limits or (mode == "cgroup2" and leaf == "/" and root.is_dir())
    numeric = [int(s) for s in limits if s.isdigit()]
    return {"uid": os.getuid(), "cpu_pair": chosen, "memory_controller": mode,
            "memory_ancestor_limits": limits, "visible_effective_limit": min(numeric, default="max"),
            "host_kernel": os.uname().release, "cgroup_sha256": sha(Path("/proc/self/cgroup")),
            "memory_kib": int(re.search(r"(?m)^MemTotal:\s*(\d+) kB$", Path("/proc/meminfo").read_text())[1])}

def applets(root, subdir, names):
    directory = root / subdir
    directory.mkdir(exist_ok=True, parents=True)
    for name in names:
        (directory / name).symlink_to("/busybox")

def runtime_libs(root, path):
    header = subprocess.check_output(["readelf", "-lW", str(path)], text=True)
    if "Requesting program interpreter:" not in header:
        return
    interpreter, = re.findall(r"Requesting program interpreter: ([^\]]+)", header)
    lines = subprocess.check_output(["ldd", str(path)], text=True)
    assert "not found" not in lines
    libraries = {interpreter, *re.findall(r"(?:=>\s+|^\s*)(/[^\s]+)\s+\(", lines, re.M)}
    for source in libraries:
        dest = root / source.lstrip("/")
        dest.parent.mkdir(exist_ok=True, parents=True)
        if not dest.exists():
            shutil.copy2(source, dest)
        assert sha(dest) == sha(Path(source))

def prepare_root(name):
    root = TMP / name
    root.mkdir()
    for d in ("proc","sys","dev","tmp","run","etc","work","records","bin","guest-rootfs/bin","guest-rootfs/tmpfs"):
        (root/d).mkdir(parents=True,exist_ok=True)
    assert sha(TMP/"busybox") == BUSYBOX
    shutil.copy2(TMP/"busybox", root/"busybox")
    shutil.copy2(TMP/"busybox", root/"guest-rootfs/busybox")
    for d in ("proc", "sys", "etc", "dev", "tmp"):
        (root / "guest-rootfs" / d).mkdir()
    for applet in ("sleep","true"):
        (root/"guest-rootfs/bin"/applet).hardlink_to(root/"guest-rootfs/busybox")
    applets(root, "bin", ("sh","cat","cp","mkdir","rm","grep","sed","stat","sleep","true","cut","sha256sum","readlink","awk","id"))
    shutil.copy2("/bin/bash", root/"bin/bash")
    runtime_libs(root, Path("/bin/bash"))
    return root

def launch(root, control):
    info = row["host"] = host_info()
    src = (TMP / "original-capsule.py").read_text()
    func, = [f for f in ast.parse(src).body if isinstance(f, ast.FunctionDef) and f.name == "launch"]
    setup = next(n.value.value for n in func.body if isinstance(n, ast.Assign) and isinstance(n.value,ast.Constant)
                 and any(isinstance(x,ast.Name) and x.id=="setup" for x in n.targets))
    assert setup.count("COMMAND") == 1
    command = "/busybox echo ROOTLESS_READY" if control else "exec /bin/bash /measure.sh"
    # The outer setup already checks the mapped IDs and the three new namespace
    # identities. Keep the inner command free of single quotes (busybox sh -ec).
    setup = setup.replace("COMMAND", command)
    namespaces = [os.readlink("/proc/self/ns/" + n) for n in ("user","net","pid")]
    cpus = ",".join(map(str,info["cpu_pair"]))
    cmd = ["taskset","-c",cpus,"/usr/bin/setpriv","--no-new-privs","/usr/bin/unshare","--user","--map-root-user","--mount","--net","--pid","--fork","--kill-child","--ipc","--uts","--propagation","private",
           "/bin/sh","-ec",setup,"guest",str(root),str(os.getuid()),*namespaces,"/usr/sbin/pivot_root"]
    env = {"PATH":"/usr/bin:/bin", "HOME":"/tmp","LANG":"C","GOTRACEBACK":"single"}
    output = run("capsule-"+STAGE,cmd,env=env)
    text = output.read_text(errors="replace")
    assert "NATIVE_GUEST_PRIVATE_ROOT" in text.splitlines()
    if control: assert "ROOTLESS_READY" in text.splitlines()

def preflight():
    for name, spec in [("original-capsule.py", CAPSULE_SOURCE + ":test/rbe/runtime_guest.py"),
                       ("original-payload.sh", VM + ":" + str(PACKAGE) + "/payload.sh")]:
        (TMP / name).write_bytes(git("show", spec))
        row[name] = sha(TMP / name)
    row["host"] = host_info()
    save()
    root = prepare_root("preflight-root")
    launch(root, True)
    row["verified"] = True

def save_phase_lines(directory: Path) -> list[tuple[Path, str]]:
    """Retain only the Sentry's numeric save messages from this checkpoint."""
    keys = ("Kernel save stats: ", "Kernel save took [", "Overall save took [",
            "Memory files save took [", "Save CPU usage: ")
    lines: list[tuple[Path, str]] = []
    for path in sorted(directory.glob("runsc.log.TestCheckpointTmpfsTreeForProfile.*.boot.txt")):
        with path.open(errors="replace") as source:
            for line in source:
                for key in keys:
                    if (at := line.find(key)) >= 0:
                        message = line[at:].strip()
                        if re.fullmatch(r"[A-Za-z0-9µ., :\[\]]+", message):
                            lines.append((path, message))
                        break
    return lines


def save_phase_times(lines: list[tuple[Path, str]]) -> tuple[dict[str, int], int, int]:
    duration = r"([0-9]+(?:\.[0-9]+)?)(ns|µs|ms|s)"
    patterns = {
        "graph": rf"Kernel save stats: ([0-9]+) objects, ([0-9]+) bytes, {duration}",
        "state": rf"Kernel save took \[{duration}\]\.",
        "kernel": rf"Overall save took \[{duration}\]\.",
        "memory": rf"Memory files save took \[{duration}\]\.",
        "cpu": rf"Save CPU usage: {duration}",
    }
    assert len({path for path, _ in lines}) == 1, lines
    scales = {"ns": 1, "µs": 1000, "ms": 1000000, "s": 1000000000}
    values = {}
    objects = encoded = 0
    for phase, pattern in patterns.items():
        matches = [m for _, text in lines if (m := re.fullmatch(pattern, text))]
        assert len(matches) == 1, (phase, lines)
        fields = matches[0].groups()
        value, unit = fields[-2:]
        values[phase] = int(Decimal(value) * scales[unit])
        if phase == "graph":
            objects, encoded = map(int, fields[:2])
    return values, objects, encoded


def bench():
    root = prepare_root("paired-root")
    built = json.loads((OUT/"build.json").read_text())
    assert built["verified"]
    shared = root / "work/test_app"
    row["executables"] = {}
    for arm in ("stock", "fixed"):
        rec = built["arms"][arm]
        dest = root/"work"/arm
        dest.mkdir()
        capsule = TMP/"live-build"/arm/"capsule.tar"
        assert sha(capsule) == rec["capsule"]
        run("unpack-"+arm, ["tar","-xf",str(capsule),"-C",str(dest)])
        owner = dest/"container_test"
        top = dest/"container_test.runfiles/_main"
        release = top/"release"
        assert (sha(owner),sha(release/"runsc"),sha(release/"gvisor-bin/gvisor_sentry")) == (rec["owner"],rec["runsc"],rec["sentry"])
        app = find_test_app(top)
        assert sha(app) == rec["test_app"]
        # ExecArgs passes this executable fd into the guest root.
        program = subprocess.check_output(["readelf", "-lW", str(app)], text=True)
        assert "Requesting program interpreter:" not in program and " LOAD " in program
        if arm == "stock":
            shared.hardlink_to(app)
        else:
            app.unlink()
            app.hardlink_to(shared)
        assert app.samefile(shared) and sha(app) == built["shared_test_app"]
        row["executables"][arm] = {"built_test_app": rec["test_app"], "executed_test_app": sha(app),
            "owner": sha(owner), "runsc": sha(release/"runsc"), "sentry": sha(release/"gvisor-bin/gvisor_sentry")}
        assert all(row["executables"][arm][k] == rec[k] for k in ("owner","runsc","sentry"))
        for exe in [owner,release/"runsc",*(release/"gvisor-bin").iterdir()]:
            if exe.is_file(): runtime_libs(root,exe)
    save()
    recipe = json.loads((OUT/"preflight.json").read_text())
    assert recipe["verified"] and all(sha(TMP/n)==recipe[n] for n in ("original-payload.sh","original-capsule.py"))
    original = (TMP/"original-payload.sh").read_text()
    body = original[original.index("for ((round=0; round<=16; ++round)); do"):]
    name = '    name="r$round-$arm"'
    assert body.count(name) == 1
    body = body.replace(name, name + '\n    export TEST_UNDECLARED_OUTPUTS_DIR="/work/$name-logs"\n    mkdir "$TEST_UNDECLARED_OUTPUTS_DIR"')
    assert body.count("cp \"$XML_OUTPUT_FILE\" \"$outer_xml\"") == 1
    body = body.replace("cp \"$XML_OUTPUT_FILE\" \"$outer_xml\"", ":")
    # Keep the failure diagnostics from the actual inner run in the test log.
    body = body.replace('if (( code != 0 )); then exit "$code"; fi',
                        'if (( code != 0 )); then /busybox tail -n 15 /work/case.log; exit "$code"; fi')
    (root/"measure.sh").write_text("""set -euo pipefail
export PATH=/bin HOME=/tmp TEST_WORKSPACE=_main TEST_TARGET=//runsc/container:container_test GO_TEST_WRAP_TESTV=1
records=/records
for cmd in bash mkdir sed stat rm sha256sum cp cat; do command -v "$cmd" > /dev/null; done
test -x /guest-rootfs/bin/sleep && /guest-rootfs/bin/true
/guest-rootfs/bin/sleep 0
for arm in stock fixed; do
  export TEST_SRCDIR="/work/$arm/container_test.runfiles" RUNFILES_DIR="/work/$arm/container_test.runfiles"
  cd "$TEST_SRCDIR/$TEST_WORKSPACE"
  app=test/cmd/test_app/test_app
  if ! test -f "$app"; then
    shopt -s nullglob
    matches=(test/cmd/test_app/*/test_app)
    test "${#matches[@]}" = 1
    app="${matches[0]}"
  fi
  test "$app" -ef /work/test_app
  /busybox sha256sum "$app" > "$records/$arm-app.sha"
  GO_TEST_WRAP=0 "/work/$arm/container_test" -test.list '^TestCheckpointTmpfsTreeForProfile$' > "$records/$arm-owner.list"
  /busybox grep -Fxq TestCheckpointTmpfsTreeForProfile "$records/$arm-owner.list"
done
/busybox sha256sum /proc/self/cgroup > "$records/cgroup.sha"
/busybox grep -Eq '^NoNewPrivs:[[:space:]]+1$' /proc/self/status
/busybox sed -n 's/^Cpus_allowed_list:[[:space:]]*//p' /proc/self/status > "$records/affinity"
""" + body)
    records = root / "records"
    observations: dict[str, list[tuple[Path, str]]] = {}
    try:
        launch(root, False)
    finally:
        original_dir = OUT / "originals"
        original_dir.mkdir(exist_ok=True)
        for path in records.iterdir():
            if re.fullmatch(r"affinity|cgroup[.]sha|(?:stock|fixed)-(?:app[.]sha|owner[.]list)|r(?:[0-9]|1[0-6])-(?:stock|fixed)[.](?:markers|exit|xml|bytes)", path.name):
                assert path.is_file() and not path.is_symlink() and path.stat().st_size < (2 << 20)
                shutil.copyfile(path, original_dir / path.name)
        for directory in sorted((root / "work").glob("r*-logs")):
            observed = observations[directory.name] = save_phase_lines(directory)
            (original_dir / (directory.name + ".txt")).write_text(
                "".join(f"{path.name}\t{message}\n" for path, message in observed))
    for arm in ("stock","fixed"):
        assert (records/(arm+"-app.sha")).read_text().split()[0] == built["shared_test_app"]
        assert "TestCheckpointTmpfsTreeForProfile" in (records/(arm+"-owner.list")).read_text().splitlines()
    assert (records / "cgroup.sha").read_text().split()[0] == row["host"]["cgroup_sha256"]
    assert host_info() == row["host"]
    affinity = (records/"affinity").read_text().strip()
    cpus = set()
    for part in affinity.split(","):
        ends = [int(x) for x in part.split("-")]
        cpus.update(range(ends[0], ends[-1]+1))
    assert cpus == set(row["host"]["cpu_pair"])
    samples = row["measurements"] = []
    for i in range(17):
        for arm in ("stock","fixed") if i == 0 or i%2 else ("fixed","stock"):
            prefix = "r%d-%s" % (i,arm)
            xml = records/(prefix+".xml")
            cases = ET.parse(xml).findall(".//testcase")
            case, = cases
            assert case.get("name","").split("/")[-1] == "TestCheckpointTmpfsTreeForProfile", case.attrib
            assert case.get("status") not in ("skipped","notrun") and case.get("result") not in ("skipped","suppressed")
            assert not any(case.find(k) is not None for k in ("skipped","error","failure"))
            assert (records/(prefix+".exit")).read_text().strip() == "0"
            lines = (records/(prefix+".markers")).read_text().splitlines()
            times = {k:int(v) for line in lines if (m:=re.fullmatch(r"TMPFS-NS (checkpoint|new|restore|new-to-guest) ([1-9][0-9]*)",line)) for k,v in [m.groups()]}
            assert len(times)==4 and times["new-to-guest"]>times["restore"] and len(lines)==6, lines
            assert {"TMPFS-GRAPH kernel=true","TMPFS-VERIFIED 50000"} <= set(lines)
            sizes = list(map(int,(records/(prefix+".bytes")).read_text().splitlines()))
            assert len(sizes)==3 and min(sizes)>0
            observed = observations[prefix+"-logs"]
            phase_ns, objects, encoded = save_phase_times(observed)
            assert 0 < phase_ns["graph"] <= phase_ns["state"] <= phase_ns["kernel"] <= times["checkpoint"], (prefix, phase_ns, times)
            assert 0 < phase_ns["memory"] <= phase_ns["kernel"] and 0 < encoded < sizes[0] and objects > 0
            assert phase_ns["cpu"] > 0, (prefix, phase_ns)
            samples.append({"round":i,"arm":arm,"times_ns":times,"bytes":sizes,"xml_sha256":sha(xml),
                            "phases_ns":phase_ns,"graph_objects":objects,"graph_bytes":encoded,
                            "boot_log_sha256":sha(observed[0][0])})
    for arm in ("stock","fixed"):
        selected = [s for s in samples if s["arm"] == arm and s["round"]]
        assert len(selected) == 16
        (OUT/(arm+".bench")).write_text("".join("BenchmarkRootlessCheckpoint 1 %d ns/op %d archive-B\n"%(s["times_ns"]["checkpoint"],sum(s["bytes"])) for s in selected))
        with (OUT/(arm+".bench")).open("a") as results:
            for sample in selected:
                for phase, ns in sample["phases_ns"].items():
                    unit = "cpu-ns/op" if phase == "cpu" else "ns/op"
                    results.write(f"BenchmarkLiveSave/{phase} 1 {ns} {unit}\n")
    row["verified"] = True

def measure():
    checkout(os.environ["GITHUB_SHA"])
    target = NATIVE
    bep = TMP/"native.bep"
    bazel("9","paired-native","test",*COMMON,"--config=x86_64","-c","opt","--strategy=TestRunner=local",
          "--nocache_test_results","--test_timeout=1500","--flaky_test_attempts=1","--test_sharding_strategy=disabled",
          "--test_env=RUNNER_TEMP="+str(TMP),"--build_event_json_file="+str(bep),target)
    with bep.open() as f:
        attempts=[(e["id"]["testResult"],e["testResult"]) for e in map(json.loads,f) if "testResult" in e]
    (ident,res), = attempts
    info = res.get("executionInfo",{})
    assert ident["label"].removeprefix("@@")==target and res["status"]=="PASSED"
    assert info.get("strategy") in ("local","standalone") and not info.get("cachedRemotely") and not res.get("cachedLocally")
    row["owner"]={"config":ident["configuration"]["id"],"strategy":info["strategy"],"label":target}
    assert json.loads((OUT/"bench.json").read_text())["verified"]
    out=run("statistics",[TMP/"benchstat","stock=stock.bench","sparse=fixed.bench"],cwd=OUT)
    shutil.copy2(out,OUT/"benchstat.txt")
    assert "RootlessCheckpoint" in out.read_text() and "n=16" in out.read_text()
    row["verified"]=True

try:
    {"preflight":preflight,"build":build,"measure":measure,"bench":bench}[STAGE]()
except Exception as e:
    row["reason"] = type(e).__name__ + ": " + str(e)[:400]
    raise
finally:
    save()
