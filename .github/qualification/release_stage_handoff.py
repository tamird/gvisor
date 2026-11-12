"""Exercise the release publisher across the two production cache mount names."""

import datetime
import difflib
import hashlib
import io
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

WORK = Path(os.environ["RELEASE_TEST"])
OUT = WORK / "public"
HOST = WORK / "host-cache"
BUILDER = Path.home() / ".cache/bazel"
TAG = "release-20261112.2"
RECORD = WORK / "calls"
NATIVE = {"x86_64": "amd64", "aarch64": "arm64"}
OPTIONS = ["-c", "opt", "--lockfile_mode=off", "--jobs=4", "--noshow_progress",
           "--remote_cache=http://cache:8080", "--remote_download_outputs=toplevel"]
STAMP = ["--config=release_version", "--embed_label=" + TAG]

def call(*args, **kw):
    return subprocess.run(args, check=True, **kw)

def output(*args):
    return subprocess.check_output(args, text=True).strip()

def write(name, body):
    p = Path(name)
    p.write_text("#!/bin/bash\nset -euo pipefail\n" + body + "\n")
    p.chmod(0o755)

def save(name, data):
    (OUT / (name + ".json")).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

def command():
    lines = Path(".buildkite/release.yaml").read_text().split('label: ":ship: Release"', 1)[1]
    commands = lines.split("    commands:\n", 1)[1].split("    agents:\n", 1)[0].splitlines()
    return "\n".join(line[8:] for line in commands if line.startswith("        ") or
                     (line.startswith("      - ") and line.strip() != "- |")).replace("$$", "$")

def docker(*args):
    return ["docker", "exec", "--user", f"{os.getuid()}:{os.getgid()}", "builder", *args]

def fixture():
    # Keep pkg_deb, its changelog and every control input intact. Small data
    # and stand-in tar bytes avoid a second full compile of both architectures.
    build = Path("debian/BUILD")
    text = build.read_text()
    for old, new in [
        ('    files = SIDECARS,', '    srcs = ["stage-fixture.txt"],'),
        ('        "//runsc",\n        "//shim:containerd-shim-runsc-v1",','        "stage-fixture.txt",'),
        ('        ":gvisor-release-tar-bz2",\n        ":gvisor-release-tar-zstd",','        ":stage-fixture-tars",')]:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    build.write_text(text + '''
genrule(
    name = "stage-fixture-tars",
    outs = ["fixture/gvisor.tar.bz2", "fixture/gvisor.tar.zstd"],
    cmd = "for f in $(OUTS); do echo " + select_arch(amd64 = "amd64", arm64 = "arm64") + " > $$f; done",
)
''')
    Path("debian/stage-fixture.txt").write_text("release mount-path qualification\n")
    write(".buildkite/scripts/release/tag.sh", 'echo "tag $1" >> "$RELEASE_TEST/calls"')
    local = Path.home() / ".local/bin"
    local.mkdir(parents=True, exist_ok=True)
    write(local / "twine", '''
[[ "$1" == upload && "$2" == --repository-url && $# == 5 ]]
[[ "$4" == "$RELEASE_TEST"/host-cache/*/release-inputs/python/*.whl ]]
[[ "$5" == "$RELEASE_TEST"/host-cache/*/release-inputs/python/*.tar.gz ]]
test -f "$4"; test -f "$5"
printf 'twine %s %s\\n' "$4" "$5" >> "$RELEASE_TEST/calls"
''')
    write(local / "gcloud", '''
if [[ "$1 $2 $3" == "artifacts versions list" ]]; then date +%Y.%m.%d.6; exit; fi
if [[ "$1 $2 $3" == "storage cp --recursive" && "$5" == gs://gvisor/releases/ ]]; then echo native >> "$RELEASE_TEST/calls"; exit; fi
if [[ "$1 $2" == "storage cp" && "$4" == gs://oss-exit-gate-prod-projects-bucket/* ]]; then
  cp "$3" "$RELEASE_TEST/public/recorded-manifest.json"
  echo manifest >> "$RELEASE_TEST/calls"; exit
fi
exit 79
''')
    write(local / "git", '''
if [[ "$1" == ls-remote && "${4:-}" == https://github.com/google/gvisor.git ]]; then exit 0; fi
exec /usr/bin/git "$@"
''')
    write(local / "make", '''
exec /usr/bin/make "$@" DOCKER_BUILD=true DOCKER_NAME=builder DOCKER_PRIVILEGED= \
    BAZEL_CACHE="$RELEASE_TEST/host-cache" \
    BAZEL_REMOTE_CACHE="--remote_cache=http://cache:8080 --jobs=4 --lockfile_mode=off --noshow_progress"
''')
    # Signed, indexed publication is genuinely exercised on the host. No
    # credentials are passed to Bazel; this records the signer input paths.
    shutil.move("tools/make_apt.sh", "tools/fixture-make-apt.sh")
    write("tools/make_apt.sh", '''
printf 'apt %s %s %s\\n' "$2" "$4" "$5" >> "$RELEASE_TEST/calls"
exec tools/fixture-make-apt.sh "$@"
''')
    call("git", "add", "debian/BUILD", "debian/stage-fixture.txt", ".buildkite/scripts/release/tag.sh",
         "tools/make_apt.sh", "tools/fixture-make-apt.sh")
    call("git", "-c", "user.name=Release Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-qm", "Apply input and endpoint fixtures in the disposable job")

def stage(kind, nightly=False):
    RECORD.write_text("")
    env = dict(os.environ, RELEASE_NIGHTLY=str(nightly).lower(),
               BUILDKITE_TAG=TAG + "-staging" if kind == "tagged" else "", BUILDKITE_BUILD_NUMBER="7")
    call("bash", "-eu", "-c", command(), env=env)
    calls = RECORD.read_text().splitlines()
    save("calls-" + kind, calls)
    prefix = [x.split()[0] for x in calls]
    assert prefix.count("native") == 1 and prefix.count("tag") == 2, calls
    tagged = kind == "tagged"
    assert prefix.count("twine") == tagged and prefix.count("manifest") == tagged, calls
    if tagged:
        manifest = json.loads((OUT / "recorded-manifest.json").read_text())
        assert ".7" in str(manifest) and "2000.1.1" not in str(manifest), manifest
        signed = {}
        for path in sorted(Path("repo/pool/20261112.2").glob("binary-*/*.deb")):
            result = output("dpkg-sig", "--verify", path)
            assert "GOODSIG" in result, result
            signed[str(path)] = result
        save("verified-package-signatures", signed)
        assert len(signed) == 2, signed
        assert prefix.index("twine") < prefix.index("native") < prefix.index("manifest"), calls
    apt = [r.split() for r in calls if r.startswith("apt ")]
    assert apt, calls
    assert all(Path(p).is_relative_to(HOST) for row in apt for p in row[2:]), apt
    assert {p.parent.name for row in apt for p in map(Path, row[2:])} == set(NATIVE)
    root = Path(apt[0][2]).parents[1]
    files = (root / "inputs").read_bytes().rstrip(b"\0").decode().split("\0")
    assert set(files) == {f"{arch}/{f}" for arch in NATIVE for f in ["runsc.deb", "gvisor.tar.bz2", "gvisor.tar.zstd"]}
    for name in files:
        p = root / name
        assert p.is_file() and not p.is_symlink(), p
        if p.suffix == ".deb":
            assert output("dpkg-deb", "-f", p, "Architecture") == NATIVE[p.parent.name]
        else:
            assert p.read_text().strip() == NATIVE[p.parent.name]
            call("sha512sum", "-c", p.name + ".sha512", cwd=p.parent)
    assert not BUILDER.exists(), "the host unexpectedly has a builder cache alias"
    have = sorted(p.name for p in (root / "python").iterdir())
    assert len(have) == (4 if tagged else 0), have
    save("stage-" + kind, dict(host=str(root), cache=str(HOST), builder=str(BUILDER), paths=files, python=have))
    return root

def control(path):
    raw = subprocess.check_output(["dpkg-deb", "--ctrl-tarfile", path])
    items = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
        for member in tar:
            data = tar.extractfile(member).read() if member.isfile() else b""
            items[member.name] = dict(type=member.type.decode(), size=member.size, uid=member.uid, gid=member.gid,
                                      mode=member.mode, mtime=member.mtime, sha256=hashlib.sha256(data).hexdigest())
    return raw, items

def original_control(raw, arch):
    # The previous full build retained only the four uncompressed control tar
    # hashes. Match those exact bytes by varying just changelog's date -R.
    previous = {
        "x86_64": {"direct":"e9522970c73f89b54fd1d134407159a6b584e5b61a2eb2bab6d7636f9f4ffca3","split":"8bc6ed68ac96f7aecb533b4a587bc72f6b04f374639cddd27f8e0b37e73bc235"},
        "aarch64":{"direct":"ba0a840675a13e79f750478f9a2f22502d2ef1d30776bc2af3ed91c8f329de57","split":"411595dba501a01cf77b833cf31552a211237e12cb8e7799c0f4df537dbcd73c"},
    }[arch]
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        item = next(m for m in tar if m.name.lstrip("./") == "changelog")
        match = re.search(rb"\w{3}, [ 0-9]\d \w{3} \d{4} \d{2}:\d{2}:\d{2} [+-]\d{4}", tar.extractfile(item).read())
        assert match
        start = item.offset_data + match.start()
        end = item.offset_data + match.end()
    found = {}
    begin = datetime.datetime(2026,11,12,7,35,tzinfo=datetime.timezone.utc)
    for seconds in range(27*60):
        timestamp = (begin + datetime.timedelta(seconds=seconds)).strftime("%a, %d %b %Y %H:%M:%S %z").encode()
        digest = hashlib.sha256(raw[:start] + timestamp + raw[end:]).hexdigest()
        for name, expected in previous.items():
            if digest == expected:
                found[name] = {"sha256": digest,"changelogDate":timestamp.decode()}
    return {"expected":previous, "exactMatches":found}

def direct(staged):
    result = {}
    for arch, debarch in NATIVE.items():
        flags = [*OPTIONS, *STAMP, "--config=" + arch]
        call(*docker("bazel", "build", *flags, "//debian:debian"))
        files = output(*docker("bazel", "cquery", *flags, "--output=files", "//debian:debian")).splitlines()
        original = output(*docker("realpath", next(p for p in files if p.endswith("/runsc.deb"))))
        host = HOST / Path(original).relative_to(BUILDER)
        # Cquery may include additional .changes artifacts, which are not published.
        rows = {}
        for kind, path in [("split", staged / arch / "runsc.deb"), ("direct", host)]:
            raw, items = control(path)
            (OUT / (debarch + "-" + kind + "-control.tar")).write_bytes(raw)
            fs = subprocess.check_output(["dpkg-deb", "--fsys-tarfile", path])
            rows[kind] = dict(controls=items, data_SHA256=hashlib.sha256(fs).hexdigest(),
                              raw_control_SHA256=hashlib.sha256(raw).hexdigest())
        left = (OUT / (debarch + "-split-control.tar")).read_bytes()
        right = (OUT / (debarch + "-direct-control.tar")).read_bytes()
        rows["historical"] = original_control(left, arch)
        changes = {}
        with tarfile.open(fileobj=io.BytesIO(left)) as a, tarfile.open(fileobj=io.BytesIO(right)) as b:
            for m in a:
                if m.isfile() and m.name in b.getnames():
                    x, y = a.extractfile(m).read(), b.extractfile(m).read()
                    if x != y:
                        changes[m.name] = list(difflib.unified_diff(x.decode(errors="replace").splitlines(), y.decode(errors="replace").splitlines(), n=1))[:12]
        rows["changedText"] = changes
        result[arch] = rows
        save("deb-control-comparison", result)
        assert rows["direct"]["data_SHA256"] == rows["split"]["data_SHA256"]

def main():
    fixture()
    for path in [Path("repo/release/20261112.2/python"), Path("artifacts/python")]:
        path.mkdir(parents=True, exist_ok=True)
        (path / "gvisor-2000.1.1.0-py3-none-any.whl").write_text("stale, must never promote\n")
    call("git", "tag", TAG)
    call("git", "tag", "staging-release-20260810.0")
    tagged = stage("tagged")
    direct(tagged)
    call(*docker("bazel", "build", *OPTIONS, *STAMP, "//debian:release-archives"))
    # Old alias target points into the builder-only cache. A caller translating
    # the stage root without resolving each old link would miss that package.
    old = output(*docker("realpath", "bazel-bin/debian/release-archives"))
    outside = HOST / Path(old).relative_to(BUILDER) / "x86_64/runsc.deb"
    save("old-absolute-link", {"host":str(outside), "target":os.readlink(outside) if outside.is_symlink() else None, "reachable":outside.exists()})
    assert outside.is_symlink() and Path(os.readlink(outside)).is_relative_to(BUILDER) and not outside.exists(), outside
    # Remove only the disposable worker's Bazel outputs; the separately mounted
    # HTTP cache was populated by its first configured build.
    call(*docker("bazel", "clean"))
    report = str(Path.cwd() / "stage-cache.bep")
    version = json.loads((OUT / "stage-tagged.json").read_text())["python"][0].split("-")[1]
    call(*docker("bazel", "build", *OPTIONS, *STAMP, "--noremote_upload_local_results",
                 "--build_event_json_file=" + report, "--//sandboxexec/sandbox/python:dist_version=" + version,
                 "//debian:release-inputs"))
    events = [json.loads(line) for line in Path(report).read_text().splitlines()]
    counters = [e["buildMetrics"].get("actionSummary", {}) for e in events if "buildMetrics" in e]
    save("remote-cache", counters)
    assert any(r.get("name") == "remote cache hit" and int(r["count"]) > 0 for c in counters for r in c.get("runnerCount", [])), counters
    stage("tagged")
    stage("tagged-nightly", nightly=True)
    call("git", "tag", "-d", TAG)
    stage("historical-nonrelease")
    call("git", "tag", "-d", "staging-release-20260810.0")
    stage("master")
    print("Validated real signing and release promotion decisions with relocated regular stage outputs.", flush=True)

if __name__ == "__main__":
    main()
