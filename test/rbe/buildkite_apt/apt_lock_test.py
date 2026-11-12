#!/usr/bin/python3
"""Run only the two unchanged shell snippets against isolated Jammy APT roots."""
import concurrent.futures
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import signal
import subprocess
import time

WORK = Path(os.environ["TEST_TMPDIR"]).resolve()
OUTPUT = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
# Bazel may resolve the executable symlink into the source tree. Resolve
# declared input data through the test's runfiles, not adjacent outputs.
HOOKS = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"] / "test/rbe/buildkite_apt"
PACKAGES = "make linux-libc-dev graphviz jq curl binutils gnupg gnupg-agent gcc pkg-config apt-transport-https ca-certificates software-properties-common rsync kmod systemd unzip".split()
KERNEL = os.uname().release
DIRECT = "linux-headers-" + KERNEL
FALLBACK = "linux-gcp-headers-" + "-".join(KERNEL.split("-")[:2])


def make_case(name, headers, missing=None):
    root = WORK / name
    for path in ("var/lib/apt/lists/partial", "var/cache/apt/archives/partial",
                 "var/lib/dpkg/updates", "var/lib/dpkg/info", "var/log/apt",
                 "etc/apt/apt.conf.d", "etc/apt/sources.list.d", "bin", "repo"):
        (root / path).mkdir(parents=True, exist_ok=True)
    (root / "etc/apt/sources.list").write_text(f"deb [trusted=yes] file:{root}/repo ./\n")
    status = root / "var/lib/dpkg/status"
    installed = [p for p in PACKAGES if p not in ("graphviz", missing)]
    def fields(p):
        return f"Package: {p}\nVersion: 1\nArchitecture: amd64\nMaintainer: Test <nobody@example.invalid>\nDescription: Fixture package {p}\n"
    status.write_text("".join("Status: install ok installed\n" + fields(p) + "\n" for p in installed))
    records = []
    for pkg in ["graphviz", *headers]:
        build = root / ("build-" + pkg)
        (build / "DEBIAN").mkdir(parents=True)
        (build / "DEBIAN/control").write_text(fields(pkg))
        deb = root / "repo" / (pkg + ".deb")
        subprocess.run(["dpkg-deb", "--build", "--root-owner-group", build, deb],
                       check=True, stdout=subprocess.DEVNULL)
        body = deb.read_bytes()
        records.append(fields(pkg) + f"Filename: ./{deb.name}\nSize: {len(body)}\nSHA256: {hashlib.sha256(body).hexdigest()}\n")
    (root / "repo/Packages").write_text("\n".join(records) + "\n")
    conf = root / "apt.conf"
    conf.write_text(f'''Dir "{root}";
Dir::State::status "{status}";
Dir::Etc::sourcelist "{root}/etc/apt/sources.list";
Dir::Etc::sourceparts "{root}/etc/apt/sources.list.d";
Dir::Etc::parts "{root}/etc/apt/apt.conf.d";
Dir::Etc::main "{root}/etc/apt/empty";
Dir::State::Lists "{root}/var/lib/apt/lists";
Dir::Cache::archives "{root}/var/cache/apt/archives";
Dir::Log "{root}/var/log/apt";
APT::Architecture "amd64";
APT::Sandbox::User "{pwd.getpwuid(os.getuid()).pw_name}";
DPkg::Options {{ "--force-not-root"; "--root={root}"; }};
''')
    # The production snippet uses sudo -E for each apt-get call. Already
    # owning the disposable root is sufficient; keep argv and exec the real APT.
    sudo = root / "bin/sudo"
    sudo.write_text('''#!/bin/bash
set -eu
[[ "$1" == "-E" && "$2" == "apt-get" ]]
printf '%s\\n' "${*:2}" >> "$APT_LOG"
shift
exec "$@"
''')
    sudo.chmod(0o755)
    return root, dict(os.environ, APT_CONFIG=str(conf), APT_LOG=str(root / "calls"),
                      PATH=str(root / "bin") + ":" + os.environ["PATH"], LC_ALL="C")


def run(name, revision, lock, headers=(), missing=None):
    root, env = make_case(name, headers, missing)
    hook = (HOOKS / (revision + "-hook")).read_text()
    prologue = hook.index("# Install packages we need.")
    block = hook[prologue:hook.index("\nset -x", prologue)]
    log = OUTPUT / (name + ".log")
    hold = (root / "var/lib/dpkg/lock-frontend").open("wb")
    if lock:
        fcntl.lockf(hold, fcntl.LOCK_EX)
    with log.open("w") as stream:
        proc = subprocess.Popen(["bash","-euc",block],env=env,stdout=stream,
                                stderr=subprocess.STDOUT,start_new_session=True)
        start = time.monotonic()
        try:
            if lock == "short":
                deadline = start + 15
                calls = root / "calls"
                while not (calls.exists() and "install" in calls.read_text()):
                    assert time.monotonic() < deadline and proc.poll() is None
                    time.sleep(0.05)
                time.sleep(2.5)
                hold.close()
            if revision == "stock" and lock == "held":
                deadline = start + 20
                while log.read_text().count("Unable to acquire the dpkg frontend lock") < 3:
                    assert time.monotonic() < deadline and proc.poll() is None, log.read_text()[-1600:]
                    time.sleep(0.05)
                os.killpg(proc.pid, signal.SIGTERM)
            code = proc.wait(timeout=145 if lock == "held" else 30)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid,signal.SIGKILL)
                proc.wait()
            hold.close()
    elapsed = time.monotonic() - start
    lines=(root / "calls").read_text().splitlines()
    updates=sum(" update " in line for line in lines)
    installs=[line for line in lines if " install " in line]
    status=(root / "var/lib/dpkg/status").read_text()
    def installed(pkg):
        return any("Package: "+pkg in p.splitlines() and "Status: install ok installed" in p.splitlines() for p in status.split("\n\n"))
    text=log.read_text()
    info=dict(name=name,exit=code,elapsed=round(elapsed,3),updates=updates,install=installs,
              dpkg_lock_errors=text.count("Unable to acquire the dpkg frontend lock"),
              graphviz=installed("graphviz"),direct=installed(DIRECT),fallback=installed(FALLBACK))
    (OUTPUT/(name+".json")).write_text(json.dumps(info,indent=2)+"\n")
    print(json.dumps(info),flush=True)
    if revision == "stock":
        assert updates >= 3 and info["dpkg_lock_errors"] >= 3 and not info['graphviz'],info
    else:
        assert updates == 1 and len(installs) == 1,info
        if lock == "held":
            assert code==100 and 115<=elapsed<145 and "Unable to acquire the dpkg frontend lock" in text and not info['graphviz'],info
        elif missing:
            assert code==100 and elapsed<15 and "Unable to locate package "+missing in text and not info['graphviz'],info
        else:
            assert code==0 and info['graphviz'] and info['direct']==(DIRECT in headers) and info['fallback']==(FALLBACK in headers and DIRECT not in headers), (info,text[-2000:])
            if lock == "short": assert 2.4<=elapsed<30,info


def main():
    assert os.geteuid()!=0, "The fixture must run as an unprivileged user"
    version = subprocess.check_output(["/usr/bin/apt-get","--version"],text=True).splitlines()[0]
    assert version.startswith("apt 2.4."),version
    print(version,flush=True)
    cases=[("stock-held","stock","held"),("bounded-held","candidate","held"),
           ("brief-lock","candidate","short",(DIRECT,FALLBACK)),
           ("fallback","candidate",None,(FALLBACK,)),("no-headers","candidate",None),
           ("missing","candidate",None,(),"make")]
    with concurrent.futures.ThreadPoolExecutor(len(cases)) as pool:
        futures=[pool.submit(run,*case) for case in cases]
        for f in futures:f.result()

if __name__=="__main__":
    main()
