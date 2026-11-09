"""Recover public release assets actually selected by these PacketImpact targets."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import time

TEMP = Path(os.environ["RUNNER_TEMP"])
OUT = TEMP / "recvbuf"
DIST = OUT / "dist"
REGISTRY = "07dee6b7b923053e558ef6cceb41f280df101fc8"
KNOWN = (
    ("protobuf", "35.1", "75b7b0b36b630c7b6e3aeb07b2e58993fb9494fbb2b08bba0891aaa231d4e4d1", 445066255),
    ("bazel_skylib", "1.9.2", "37cdfbc6faefea94f7b37760a305c98c08981116c2bc9e821e3b423221fad8c8", 480693316),
    ("rules_android", "0.7.1", "7c45b6aaa837fb6f2f23ad11387638cb00fa9f839a04ec564caac70a543a9cd5", 341081786),
)
URL = re.compile(r"https://github[.]com/([\w.-]+)/([\w.-]+)/releases/download/([\w.+-]+)/([\w.+-]+)")
DIR = re.compile(r"/external/([A-Za-z0-9_.-]+)[+]/")
LABELS = ["//test/packetimpact/tests:tcp_rcv_buf_space_" + name + "_test" for name in ("native", "netstack")]


def fetch(url, limit, *, asset=False):
    args = ["curl", "-fsSL", "--max-time", "80", "--retry", "1", "--max-filesize", str(limit)]
    if asset:
        args += ["-H", "Accept: application/octet-stream"]
    cmd = subprocess.run([*args, url], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return cmd.stdout if cmd.returncode == 0 and len(cmd.stdout) <= limit else b""


def acquire(module, version, *, wanted_url=None, pinned=None, asset_id=None):
    for component in (module, version):
        assert re.fullmatch(r"[A-Za-z0-9_.+-]+", component) and component not in (".", "..")
    src = json.loads(fetch(f"https://raw.githubusercontent.com/bazelbuild/bazel-central-registry/{REGISTRY}/modules/{module}/{version}/source.json", 256 << 10))
    url = src["url"]
    match = URL.fullmatch(url)
    assert match and (not wanted_url or url == wanted_url), (module, version, url)
    scheme, encoded = src["integrity"].split("-",1)
    digest = base64.b64decode(encoded, validate=True).hex()
    assert scheme == "sha256" and len(digest) == 64 and (not pinned or digest == pinned)
    owner, repo, tag, name = match.groups()
    assert name not in (".","..") and len(name) < 160
    output = DIST / name
    if output.exists():
        assert hashlib.sha256(output.read_bytes()).hexdigest() == digest
        return None
    blob = fetch("https://mirror.bazel.build/" + url.removeprefix("https://"), 100 << 20)
    via = "bazel-mirror"
    if not blob:
        if asset_id is None:
            rel = json.loads(fetch(f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}", 8 << 20))
            (item,) = [v for v in rel["assets"] if v["name"] == name and v["browser_download_url"] == url]
            asset_id = int(item["id"])
            assert 0 < item["size"] <= 100 << 20
        blob = fetch(f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{asset_id}",
                     100 << 20, asset=True)
        via = "public-github-asset"
    actual = hashlib.sha256(blob).hexdigest()
    assert blob and actual == digest, (module, version, name, actual)
    output.write_bytes(blob)
    return dict(module=module, version=version, registry=REGISTRY, source=url, via=via,
                asset_id=asset_id, bytes=len(blob), sha256=actual)


# These are the module versions actually requested in the first two original
# attempts. Each optional distfile still has to match the immutable registry.
OBSERVED = (
    ("rules_python", "2.4.5"), ("toml.bzl", "0.4.1"),
    ("bazel_lib", "3.4.0"), ("gazelle", "0.51.3"),
    ("package_metadata", "0.0.10"), ("rules_cc", "0.2.25"),
    ("rules_pkg", "1.0.1"), ("rules_proto", "7.1.0"), ("rules_shell", "0.8.0"),
)


def save(report):
    (OUT / "dependencies.json").write_text(json.dumps(report, indent=2) + "\n")


def seed():
    DIST.mkdir()
    report = {"verified": False, "registry": REGISTRY, "assets": [], "attempts": []}
    for module, version, digest, asset in [*KNOWN, *((m, v, None, None) for m, v in OBSERVED)]:
        try:
            info = acquire(module, version, pinned=digest, asset_id=asset)
            report["assets"].append(info)
        except Exception as exc:
            report.setdefault("unavailable_seeds", []).append([module, version, type(exc).__name__])
        save(report)


def tool_asset(url):
    """Admit these non-module distfiles only after this build requested them."""
    owner, repo, tag, name = URL.fullmatch(url).groups()
    if url == "https://github.com/protocolbuffers/protobuf/releases/download/v35.1/protoc-35.1-linux-x86_64.zip":
        source = DIST / "protobuf-35.1.bazel.tar.gz"
        assert hashlib.sha256(source.read_bytes()).hexdigest() == KNOWN[0][2]
        with tarfile.open(source) as tar:
            metadata = tar.extractfile("protobuf-35.1/bazel/private/oss/toolchains/prebuilt/tool_integrity.bzl").read().decode()
        (digest,) = re.findall(r'^  "protoc-35[.]1-linux-x86_64[.]zip": "([0-9a-f]{64})",?$', metadata, re.M)
        asset, size = 445060247, 3633928
    elif url == "https://github.com/cerisier/kernel-headers/releases/download/6.18.33-20260526/6.18.33-arm64.tar.zst":
        assert 'bazel_dep(name = "llvm", version = "0.8.24")' in Path("MODULE.bazel").read_text()
        index = fetch("https://raw.githubusercontent.com/hermeticbuild/hermetic-llvm/v0.8.24/kernel/extension/kernel_headers_index.json", 4 << 20)
        assert re.search(r'\{\s*"url": "' + re.escape(url) + r'",\s*"sha256": "b00c090507fe0ace68f322894822fc27379fc872f10eaa71525adcc18a6c56c9"\s*\}', index.decode())
        digest, asset, size = "b00c090507fe0ace68f322894822fc27379fc872f10eaa71525adcc18a6c56c9", 430265983, 1500852
    else:
        return None
    output = DIST / name
    if output.exists():
        assert hashlib.sha256(output.read_bytes()).hexdigest() == digest
        return None
    blob = fetch(f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{asset}", size, asset=True)
    assert len(blob) == size and hashlib.sha256(blob).hexdigest() == digest
    output.write_bytes(blob)
    return dict(source=url, via="supplier-index-and-public-asset", asset_id=asset, bytes=size, sha256=digest)


def prepare():
    # Only the Bazel server receives the private rc. The TestRunner starts in
    # the following workflow step after shutdown and removal of the private rc.
    private = OUT / "auth"
    report = json.loads((OUT / "dependencies.json").read_text())
    report.update(remote_downloader="grpcs://remote.buildbuddy.io", remote_execution=False,
                  operation="build --nobuild", phase="resolving")
    save(report)
    args = [str(TEMP / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g",
            "--bazelrc=" + str(private / "service.rc"), "build", "--nobuild", "--config=x86_64", "--jobs=3",
            "--strategy=TestRunner=local", "--test_env=GO_TEST_WRAP_TESTV=1", "--nocache_test_results",
            "--test_sharding_strategy=disabled", "--runs_per_test=4", "--flaky_test_attempts=1",
            "--test_timeout=360", "--test_filter=^TestReduceRecvBuf$", "--zip_undeclared_test_outputs",
            "--@io_bazel_rules_go//go/config:pure", "--keep_going", "--remote_executor=", "--bes_backend=", "--distdir=" + str(DIST),
            "--noremote_upload_local_results", "--noremote_accept_cached", *LABELS]
    recovered = set()
    while True:
        log = private / "build.log"
        started = time.monotonic()
        with log.open("wb") as stream:
            rc = subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT).returncode
        row = {"exit": rc, "labels": LABELS, "seconds": round(time.monotonic() - started, 2)}
        report["attempts"].append(row)
        if rc == 0:
            report["phase"] = "analysis complete"
            report["verified"] = True
            save(report)
            return 0
        # Only public module release coordinates and fixed diagnostic classes
        # can pass from the private build log to the qualification attachment.
        seen = set()
        classes = set()
        tools_requested = set()
        failed_owners = set()
        for line in log.read_text(errors="replace").splitlines():
            for signal in ("PERMISSION_DENIED", "UNAUTHENTICATED", "UNAVAILABLE",
                           "DEADLINE_EXCEEDED", "HTTP 504", "Error downloading",
                           "error executing", "No space left", "OutOfMemoryError"):
                if signal in line:
                    classes.add(signal)
            if line.startswith("ERROR:"):
                failed_owners.update(re.findall(r"(?<![A-Za-z0-9/_])//([A-Za-z0-9_/.-]+:[A-Za-z0-9_.+-]+)", line))
            if "Error downloading" not in line:
                continue
            modules = set(DIR.findall(line))
            urls = {m.group(0) for m in URL.finditer(line)}
            tools_requested.update(urls)
            if len(modules) == len(urls) == 1:
                seen.add((next(iter(modules)), next(iter(urls))))
        row.update(diagnostics=sorted(classes), missing_bcr=[])
        row["failed_owners"] = sorted(failed_owners)[:12]
        progress = False
        for url in sorted(tools_requested):
            try:
                info = tool_asset(url)
                if info:
                    report["assets"].append(info)
                    progress = True
            except Exception as exc:
                row.setdefault("unavailable_tools", []).append([URL.fullmatch(url).group(4), type(exc).__name__])
        for module, url in sorted(seen - recovered):
            recovered.add((module, url))
            version = URL.fullmatch(url).group(3).removeprefix("v")
            try:
                # acquire verifies the public URL and checksum from BCR
                # before admitting either the archive or URL to our report.
                info = acquire(module, version, wanted_url=url)
                if info:
                    report["assets"].append(info)
                    row["missing_bcr"].append([module, version])
                    progress = True
            except Exception:
                row.setdefault("unverified_module", []).append(module)
        save(report)
        if not progress:
            return rc


if __name__ == "__main__":
    if sys.argv[1:] == ["seed"]:
        seed()
    elif sys.argv[1:] == ["prepare"]:
        raise SystemExit(prepare())
    else:
        raise SystemExit("expected seed or prepare")
