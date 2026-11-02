"""Recheck the exact gVisor Gazelle patch on the existing minimal client."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import datetime
import sys
import zipfile

SOURCE = "2b09bdf5dd2be338899abb48753c27cd9e80ba22"
BAZEL = "5b5bab2095065817d3416ca9a0678bd39c10c9ede432dbe8187bfe74de285160"


def reconcile_original():
    """Classify the observed process-wrapper error without repeating a query."""
    temporary = Path(os.environ["RUNNER_TEMP"])
    archive = (temporary / "gazelle.zip").read_bytes()
    sha = hashlib.sha256(archive).hexdigest()
    assert sha == "9c47e13b07ebfaa3435c79b33c70fc3fc3be8044a8a0b1521e699ab559b3798f"
    metadata = json.loads((temporary / "gazelle-artifact.json").read_text())
    assert metadata["id"] == 13167514851 and metadata["size_in_bytes"] == len(archive)
    assert metadata["digest"] == "sha256:" + sha
    assert metadata["workflow_run"]["id"] == 41501536218
    assert metadata["workflow_run"]["head_sha"] == "999b71a831bb3bc743a1d8e59c2484a54284ef91"
    with zipfile.ZipFile(temporary / "gazelle.zip") as bundle:
        assert sorted(bundle.namelist()) == ["minimal-files.json", "verdict.json"]
        original = json.loads(bundle.read("verdict.json"))
        inventory = json.loads(bundle.read("minimal-files.json"))
    assert not any(path in inventory for path in ("bin/env", "usr/bin/env"))
    assert original["source"] == SOURCE and original["status"] == "INCOMPLETE"
    for field, path in (("module_sha256", "MODULE.bazel"), ("patch_sha256", "tools/gazelle-native-environment.patch")):
        data = subprocess.check_output(["git", "show", SOURCE + ":" + path])
        assert original[field] == hashlib.sha256(data).hexdigest()
    phases = original["phases"]
    assert list(phases) == ["stock", "stock-with-env", "candidate", "leaking-control", "bazel7-minimum"]
    stock = phases["stock"]
    assert stock["status"] == "INCOMPLETE" and stock["error"] == "AssertionError" and stock["exit"] == 7
    assert any('"execvp(env, ...)": No such file or directory' in line for line in stock["diagnostics"])
    assert all(phases[name]["status"] == "PASS" for name in list(phases)[1:])
    assert stock["effective_helper_sha256"] == phases["stock-with-env"]["effective_helper_sha256"]
    assert phases["candidate"]["patch_sha256"] == original["patch_sha256"]
    expected = {"GAZELLE_FIX_EXPLICIT": "child", "GAZELLE_FIX_OVERRIDDEN": "child", "GAZELLE_FIX_EMPTY": "", "TMP": "/tmp", "TMPDIR": "/tmp"}
    assert all(phases[name]["exit"] == 0 and phases[name]["child_environment"] == expected for name in ("candidate", "stock-with-env"))
    assert phases["leaking-control"]["exit"] == 7 and any("child environment differs:" in line and "GAZELLE_FIX_AMBIENT" in line for line in phases["leaking-control"]["diagnostics"])
    assert phases["bazel7-minimum"]["exit"] == 48 and any('module "<root>" (bazel_compatibility: [>=8.0.0])' in line for line in phases["bazel7-minimum"]["diagnostics"])
    destination = temporary / "unit-reconciled"
    destination.mkdir(exist_ok=True)
    value = dict(status="RECOVERED", source=SOURCE, original_archive_sha256=sha, original_actions_conclusion="failure",
                 original=original, corrected_phase_status={name: "PASS" for name in phases},
                 intervention="Accept the actual process-wrapper execvp(env) ENOENT diagnostic; no query or build repeated",
                 recovered_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    (destination / "gazelle-reconciliation.json").write_text(json.dumps(value, indent=2) + "\n")
    print(json.dumps({key: value[key] for key in ("status", "source", "recovered_at")}))


if sys.argv[1:] == ["--reconcile-original"]:
    reconcile_original()
    sys.exit(0)

tree = Path.cwd()
assert os.uname().machine == "x86_64"
assert set(subprocess.check_output(["git", "diff", "--name-only", SOURCE, "HEAD"], text=True).splitlines()) == {".github/qualification/gazelle-native-minimum.py", ".github/workflows/gazelle-native-minimum.yml"}
assert not subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], text=True)
staging = Path(tempfile.mkdtemp(dir=os.environ["RUNNER_TEMP"], prefix="repo-fetch-"))
evidence = Path(os.environ["RUNNER_TEMP"]) / "gazelle-minimum"
evidence.mkdir()
root = staging / "image"
root.mkdir()
def cmd(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)
def digest(data):
    return hashlib.sha256(data).hexdigest()
def add_file(path, target=None):
    dest = root / str(target or path).lstrip("/")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
client = staging / "bazel"
cmd("curl", "-fsSL", "--retry", "3", "-o", client, "https://releases.bazel.build/8.8.1/release/bazel-8.8.1-linux-x86_64")
assert digest(client.read_bytes()) == BAZEL
client.chmod(0o755)
add_file(client, "/probe/bazel")
# The Bazel native launcher and its embedded JDK are explicitly allowed.
# Include shared libraries only; copying whole /usr/lib would also supply git.
for soname in ("ld-linux-x86-64.so.2", "libc.so.6", "libm.so.6", "libz.so.1",
               "libdl.so.2", "libpthread.so.0", "libgcc_s.so.1", "libstdc++.so.6",
               "libresolv.so.2", "librt.so.1", "libutil.so.1", "libnss_files.so.2",
               "libnss_dns.so.2", "libtinfo.so.6", "libcrypt.so.1", "libzstd.so.1"):
    src = Path("/lib/x86_64-linux-gnu") / soname
    if src.exists():
        add_file(src)
add_file("/lib64/ld-linux-x86-64.so.2")
add_file("/etc/ssl/certs/ca-certificates.crt")
jvm_extra = []
for trust in (Path("/etc/ssl/certs/java/cacerts"), Path(os.environ.get("JAVA_HOME", "/usr/lib/jvm/default-java")) / "lib/security/cacerts"):
    if trust.is_file():
        add_file(trust, "/trust/cacerts")
        jvm_extra = ["--host_jvm_args=-Djavax.net.ssl.trustStore=/trust/cacerts"]
        break
(root / "etc/passwd").write_text(f"runner:x:{os.getuid()}:{os.getgid()}:runner:/home/runner:/nonexistent\n")
(root / "etc/group").write_text(f"runner:x:{os.getgid()}:\n")
(root / "etc/nsswitch.conf").write_text("passwd: files\ngroup: files\nhosts: files dns\n")
for directory in ("bin", "usr/bin", "work", "output", "repository-cache", "home/runner", "tmp"):
    (root / directory).mkdir(parents=True, exist_ok=True)
(root / "tmp").chmod(0o1777)
clients = {"8.8.1": "/probe/bazel"}
for ver, expected in (("7.7.0", "fe7e799cbc9140f986b063e06800a3d4c790525075c877d00a7112669824acbf"),):
    client_path = staging / f"bazel-{ver}"
    cmd("curl", "-fsSL", "--retry", "3", "-o", client_path, f"https://releases.bazel.build/{ver}/release/bazel-{ver}-linux-x86_64")
    assert digest(client_path.read_bytes()) == expected
    client_path.chmod(0o755)
    add_file(client_path, f"/probe/bazel-{ver}")
    clients[ver] = f"/probe/bazel-{ver}"
inventory = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
(evidence / "minimal-files.json").write_text(json.dumps(inventory, indent=2))
with (staging / "root.tar").open("wb") as f, tarfile.open(fileobj=f, mode="w") as tf:
    tf.add(root, arcname=".")
cmd("docker", "import", staging / "root.tar", "repo-fetch:local")
def mounts(base, normal=False, helpers=()):
    args = ["docker", "run", "--rm", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only", "--tmpfs", "/tmp:rw,exec,mode=1777", "--user", f"{os.getuid()}:{os.getgid()}", "--workdir", "/work", "--env", "PATH=/usr/local/bin:/usr/bin:/bin", "--env", "HOME=/home/runner"]
    for local, dest in (("work", "/work"), ("output", "/output"), ("repository-cache", "/repository-cache"), ("home", "/home/runner")):
        args += ["--volume", f"{base/local}:{dest}"]
    for src, dest in helpers:
        args += ["--volume", f"{src}:{dest}:ro"]
    if normal:
        for directory in ("/usr", "/bin", "/lib", "/lib64"):
            args += ["--volume", f"{directory}:{directory}:ro"]
    return args + ["repo-fetch:local"]
def prepare(name):
    base = staging / name
    for folder in ("work", "output", "repository-cache", "home"):
        (base / folder).mkdir(parents=True)
    return base
def startup(version):
    return [clients[version], "--batch", "--nosystem_rc", "--nohome_rc", "--output_user_root=/output", *jvm_extra]
check = '''load("@bazel_gazelle//internal:common.bzl", "env_execute")
def _check(ctx):
    result = env_execute(ctx, ["/fixture/printenv"], environment = {
        "GAZELLE_FIX_EXPLICIT": "child",
        "GAZELLE_FIX_OVERRIDDEN": "child",
        "GAZELLE_FIX_EMPTY": "",
        "TMP": "/tmp",
    })
    if result.return_code:
        fail(result.stderr)
    values = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key.startswith("GAZELLE_FIX_") or key in ["TMP", "TMPDIR"]:
            values[key] = value
    want = {
        "GAZELLE_FIX_EXPLICIT": "child", "GAZELLE_FIX_OVERRIDDEN": "child",
        "GAZELLE_FIX_EMPTY": "", "TMP": "/tmp", "TMPDIR": "/tmp",
    }
    if values != want:
        fail("child environment differs: " + repr(values))
    ctx.file("BUILD.bazel", "exports_files(['verdict'])")
    ctx.file("verdict", json.encode(values))
check = repository_rule(implementation = _check)
'''
module = (tree / 'MODULE.bazel').read_text()
header = module.split('\n)\n', 1)[0] + '\n)\n'
assert 'bazel_compatibility = [">=8.0.0"]' in header
version, = [line for line in module.splitlines() if line.startswith('bazel_dep(name = "gazelle",')]
assert 'version = "0.51.3"' in version
start = module.index('single_version_override(\n    module_name = "gazelle",')
override = module[start:module.index('\n)', start) + 2]
patch = (tree / 'tools/gazelle-native-environment.patch').read_bytes()
report = {'source': SOURCE, 'status': 'INCOMPLETE', 'module_sha256': digest(module.encode()),
          'patch_sha256': digest(patch), 'bazel': {'8.8.1': BAZEL, '7.7.0': expected},
          'scope': 'Actual patched Gazelle helper and isolated gVisor module declaration; no product compilation',
          'phases': {}}
def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
def save():
    (evidence / 'verdict.json').write_text(json.dumps(report, indent=2) + '\n')
save()
want = {'GAZELLE_FIX_EXPLICIT': 'child', 'GAZELLE_FIX_OVERRIDDEN': 'child',
        'GAZELLE_FIX_EMPTY': '', 'TMP': '/tmp', 'TMPDIR': '/tmp'}
for name in ['stock', 'stock-with-env', 'candidate', 'leaking-control', 'bazel7-minimum']:
    row = report['phases'][name] = {'status': 'INCOMPLETE', 'start': now()}
    save()
    try:
        base = prepare(name)
        work = base / 'work'
        helpers = [('/usr/bin/printenv', '/fixture/printenv')]
        if name == 'stock-with-env':
            helpers.append(('/usr/bin/env', '/usr/bin/env'))
        boundary = name == 'bazel7-minimum'
        text = header
        if not boundary:
            text += version + '\n'
            if name in ('candidate', 'leaking-control'):
                text += override + '\n'
                (work / 'tools').mkdir()
                (work / 'tools/BUILD').write_text('exports_files(["gazelle-native-environment.patch"])\n')
                data = patch
                if name == 'leaking-control':
                    assert data.count(b'overrides = {key: None for key in ctx.os.environ}') == 1
                    data = data.replace(b'overrides = {key: None for key in ctx.os.environ}', b'overrides = {}')
                (work / 'tools/gazelle-native-environment.patch').write_bytes(data)
                row['patch_sha256'] = digest(data)
            text += 'check = use_repo_rule("//:check.bzl", "check")\ncheck(name = "child")\n'
            (work / 'check.bzl').write_text(check)
        (work / 'MODULE.bazel').write_text(text)
        (work / 'BUILD.bazel').write_text('exports_files(["MODULE.bazel"])\n')
        row['fixture_module_sha256'] = digest(text.encode())
        args = mounts(base, helpers=helpers) + startup('7.7.0' if boundary else '8.8.1') + [
            'query', '--repository_cache=/repository-cache', '--color=no', '--curses=no',
            '--repo_env=GAZELLE_FIX_AMBIENT=must-not-reach-go', '--repo_env=GAZELLE_FIX_OVERRIDDEN=parent',
            '--repo_env=GAZELLE_FIX_EMPTY=parent', '--repo_env=TMPDIR=/tmp', '--repo_env=TMP=/parent-tmp',
            '//:MODULE.bazel' if boundary else '@child//:verdict']
        try:
            result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=240)
            row['exit'], output = result.returncode, result.stdout
        except subprocess.TimeoutExpired as error:
            row['exit'], output = 124, error.stdout or b''
        row['finish'] = now()
        row['output_sha256'] = digest(output)
        log = output.decode(errors='replace')
        # No credentials enter these containers. Keep only bounded failure lines.
        row['diagnostics'] = [line[:1000] for line in log.splitlines() if any(marker in line for marker in (
            'ERROR:', 'Error in ', 'Cannot run program', 'not found in PATH', 'child environment differs',
            'requires Bazel', 'Bazel compatibility'))][:20]
        (base / 'output.log').write_bytes(output)
        effective = list((base / 'output').glob('*/external/*gazelle*/internal/common.bzl'))
        if not boundary:
            assert len(effective) == 1, [str(path) for path in effective]
            row['effective_helper_sha256'] = digest(effective[0].read_bytes())
        if name in ('candidate', 'stock-with-env'):
            assert row['exit'] == 0, row
            verdicts = list((base / 'output').glob('*/external/*child/verdict'))
            assert len(verdicts) == 1, [str(path) for path in verdicts]
            row['child_environment'] = json.loads(verdicts[0].read_text())
            assert row['child_environment'] == want
        elif name == 'stock':
            assert row['exit'] not in (0, 124) and any(error in log for error in ('Cannot run program "env"', 'env is not found in PATH', '"execvp(env, ...)": No such file or directory')), row
        elif name == 'leaking-control':
            assert row['exit'] not in (0, 124) and 'child environment differs:' in log and 'GAZELLE_FIX_AMBIENT' in log, row
        else:
            expected_error = 'Bazel version 7.7.0 is not compatible with module "<root>" (bazel_compatibility: [>=8.0.0])'
            assert row['exit'] not in (0, 124) and expected_error in log and 'Bazel compatibility check failed' in log, row
        row['status'] = 'PASS'
    except Exception as error:
        row['error'] = type(error).__name__
    save()
    print(name, row['status'], row.get('exit'), row.get('diagnostics'), flush=True)
assert not subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], text=True)
if all(row['status'] == 'PASS' for row in report['phases'].values()):
    assert report['phases']['stock']['effective_helper_sha256'] == report['phases']['stock-with-env']['effective_helper_sha256']
    report['status'] = 'PASS'
report['verdict_utc'] = now()
save()
raise SystemExit(report['status'] != 'PASS')
