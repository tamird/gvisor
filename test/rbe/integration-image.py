"""Qualify the existing integration image consumers with declared package inputs."""

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET
import zipfile

PRODUCT = "fa1b424ffda1cecb818adffcaf46cfd3e0fb0028"
AUDITOR = "9041e61701db27bd85b9658385cf5d7803681ab1"
SUPPLIER = "af1089c91c36bfc78719b2836c73d374a66ef092"
TMP = Path(os.environ["RUNNER_TEMP"])
PRIVATE = TMP / "integration-private"
PUBLIC = TMP / "integration-public"
PRIVATE.mkdir(exist_ok=True)
PUBLIC.mkdir(exist_ok=True)
BZ = ["bazel", "--bazelrc=" + str(TMP / "images.bazelrc")]
RESULT = {"product": PRODUCT, "carrier": os.environ["GITHUB_SHA"], "phases": {}}
TAG = "gvisor.dev/images/basic/integrationtest:latest"
HELPERS = ["host_connect", "host_fd", "link_test", "tcp_loopback", "tcp_server",
           "tcp_stress_server", "test_copy_up", "test_rewinddir", "test_sticky"]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save() -> None:
    data = json.dumps(RESULT, indent=2).encode() + b"\n"
    for key, value in os.environ.items():
        if ("TOKEN" in key or "SECRET" in key or "API_KEY" in key) and len(value) >= 12:
            assert value.encode() not in data, f"Secret in report: {key}"
    rc = TMP / "images.bazelrc"
    if rc.exists():
        for line in rc.read_text().splitlines():
            if "x-buildbuddy-api-key=" in line:
                assert line.split("x-buildbuddy-api-key=", 1)[1].encode() not in data
    assert b"x-buildbuddy-api-key" not in data and b"Bearer " not in data
    (PUBLIC / "result.json").write_bytes(data)


def run(name: str, args: list[str], row: dict) -> subprocess.CompletedProcess:
    p = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (PRIVATE / (name + ".log")).write_text(p.stdout)
    item = {"command": args, "exit": p.returncode, "finished": now(), "log_sha256": sha(p.stdout.encode())}
    row.setdefault("commands", []).append(item)
    ids = re.findall(r"https://app.buildbuddy.io/invocation/([a-f0-9-]{36})", p.stdout)
    item["invocations"] = sorted(set(ids))
    if p.returncode:
        lines = p.stdout.splitlines()
        first = next((i for i, line in enumerate(lines)
                      if line.startswith(("ERROR:", "FAIL:", "licensecheck:"))), len(lines))
        item["diagnostics"] = [line[:1200] for line in lines[first:first + 60]
                               if not any(word in line.lower() for word in
                                          ("authorization", "x-buildbuddy-api-key", "bearer "))]
        item["diagnostics_bounded"] = first + 60 < len(lines)
    save()
    return p


def checked(name: str, args: list[str], row: dict) -> str:
    p = run(name, args, row)
    assert p.returncode == 0, f"{name} exited {p.returncode}"
    return p.stdout


def outputs(name: str, target: str, flags: list[str], row: dict) -> list[Path]:
    text = checked(name, BZ + ["cquery", *flags, "--output=files", target], row)
    paths = [Path(line) for line in text.splitlines() if line.startswith(("bazel-out/", "external/"))]
    assert paths, "No selected output files"
    if any(not path.is_file() for path in paths):
        text = checked(name + "-root", BZ + ["info", "execution_root"], row)
        roots = [Path(line) for line in text.splitlines() if line.startswith("/")]
        assert len(roots) == 1
        paths = [path if path.is_file() else roots[0] / path for path in paths]
    assert all(path.is_file() for path in paths)
    return paths


def one(name: str, target: str, flags: list[str], row: dict) -> Path:
    paths = outputs(name, target, flags, row)
    assert len(paths) == 1, paths
    return paths[0]


def checkout(source: str, row: dict) -> None:
    checked("checkout", ["git", "checkout", "--quiet", "--detach", source], row)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == source
    assert not subprocess.check_output(["git", "diff", "HEAD", "--"], text=True)


def auditor(row: dict) -> None:
    checkout(AUDITOR, row)
    target = "//tools/licensecheck/main:licensecheck"
    checked("auditor-build", BZ + ["build", target], row)
    binary = one("auditor-path", target, [], row)
    shutil.copyfile(binary, PRIVATE / "licensecheck")
    (PRIVATE / "licensecheck").chmod(0o755)
    row["source"] = AUDITOR
    row["elf_sha256"] = sha(binary.read_bytes())


def catalog(row: dict) -> None:
    checkout(PRODUCT, row)
    os.environ["MAKEFLAGS"] = "DOCKER_BUILD=false STARTUP_OPTIONS=--bazelrc=" + str(TMP / "images.bazelrc")
    p = run("catalog", [str(PRIVATE / "licensecheck"), "--mode=verify"], row)
    text = p.stdout
    names = set(re.findall(r"(?m)^- dependency: (.+)$", Path("tools/licensecheck/dependencies.yaml").read_text()))
    problems = [line for line in text.splitlines()
                if re.fullmatch(r"(?:stale|missing|duplicate) entry for [A-Za-z0-9_./+@-]+", line)
                or line.split(" ", 1)[0].removesuffix(":") in names]
    assert all(len(line) <= 2000 for line in problems)
    row["problems"] = problems
    count = re.findall(r"licensecheck: (\d+) problems;", text)
    row["diagnostics_complete"] = len(count) == 1 and int(count[0]) == len(problems) if p.returncode else True
    assert p.returncode == 0, f"catalog exited {p.returncode}"
    lines = [line for line in text.splitlines() if re.fullmatch(r"all \d+ dependencies have up-to-date license entries that conform to governance/licensing.yaml", line)]
    assert len(lines) == 1, "Canonical complete-catalog success line missing"
    row["verification"] = lines[0]
    row["auditor_source"] = AUDITOR
    row["catalog_sha256"] = sha(Path("tools/licensecheck/dependencies.yaml").read_bytes())
    row["policy_sha256"] = sha(Path("governance/licensing.yaml").read_bytes())


def prerequisite(row: dict) -> None:
    checkout(PRODUCT, row)
    path = Path("MODULE.bazel")
    text = path.read_text()
    row["product_module_sha256"] = sha(text.encode())
    text = text.replace("image_apt.sources_list(\n", "image_apt.sources_list(\n    gpg_keys = [\"@integration_ubuntu_archive_keys//file:ubuntu-archive-keyring.gpg\"],\n", 1)
    text += '\n# Qualification-only signed-source input for the newer supplier.\n'
    text += 'http_file(\n    name = "integration_ubuntu_archive_keys",\n'
    text += '    urls = ["https://archive.ubuntu.com/ubuntu/project/ubuntu-archive-keyring.gpg"],\n'
    text += '    sha256 = "80a36b0a6de2f69f49d2df75ef473ccde121e9e190b9ea01d20a4f63778d5c31",\n'
    text += '    downloaded_file_path = "ubuntu-archive-keyring.gpg",\n)\n'
    text += '\n# Qualification-only pending installed-package inventory repair (#294).\n'
    text += 'archive_override(\n    module_name = "rules_distroless",\n'
    text += '    urls = ["https://github.com/tamird/rules_distroless/archive/' + SUPPLIER + '.tar.gz"],\n'
    text += '    strip_prefix = "rules_distroless-' + SUPPLIER + '",\n'
    text += '    integrity = "sha256-byvYMoZBAKqzZI+xkMZAMuYd16XyJkGsvPuKuiNopDM=",\n)\n'
    path.write_text(text)
    row["supplier_source"] = SUPPLIER
    row["supplier_archive_sha256"] = "6f2bd832864100aab3648fb190c64032e61dd7a5f22641acbcfb8aba2368a433"
    row["qualified_module_sha256"] = sha(text.encode())
    assert subprocess.check_output(["git", "diff", "--name-only"], text=True).splitlines() == ["MODULE.bazel"]


def original(row: dict) -> None:
    path = TMP / "original-integration.zip"
    assert sha(path.read_bytes()) == "82be788bfb0930a5c14046bac85a895bf1faefc4775e8be121d7a9690ea12f3a"
    with zipfile.ZipFile(path) as z:
        assert z.namelist() == ["result.json"]
        old = json.loads(z.read("result.json"))
    assert old["product"] == PRODUCT and old["carrier"] == "17cd1ea8c44addf12b45de23d1a88c70ab8c12e1"
    assert old["status"] == "FAIL" and old["phases"]["auditor"]["status"] == "PASS"
    assert old["phases"]["catalog"]["commands"][-1]["exit"] == 1
    for name in ("amd64", "arm64", "runtime"):
        command = old["phases"][name]["commands"][-1]
        assert command["exit"] == 37 and any("Expected a file with" in line for line in command["diagnostics"])
    RESULT["original"] = old
    row["artifact"] = "https://github.com/tamird/gvisor/actions/runs/41523954650/artifacts/13178134790"
    row["archive_sha256"] = sha(path.read_bytes())


def prior_catalog(row: dict) -> None:
    path = TMP / "prior-preflight.zip"
    assert sha(path.read_bytes()) == "870922606cdb3c7eecea8237fa9b0aa54e70baa9361ed46bcbe0b71656fbf09d"
    with zipfile.ZipFile(path) as z:
        assert z.namelist() == ["result.json"]
        old = json.loads(z.read("result.json"))
    assert old["product"] == PRODUCT and old["carrier"] == "b9f0559251581519fb3ff0f08e06a5f7dd8f6c94"
    assert old["status"] == "FAIL" and old["original"] == RESULT["original"]
    assert old["phases"]["auditor"]["status"] == "PASS"
    assert old["phases"]["auditor"]["source"] == AUDITOR
    catalog = old["phases"]["catalog"]
    assert catalog["status"] == "PASS" and catalog["commands"][-1]["exit"] == 0
    assert catalog["verification"] == "all 350 dependencies have up-to-date license entries that conform to governance/licensing.yaml"
    assert old["phases"]["resolve"]["commands"][-1]["exit"] == 1
    assert "runtime_not_run" in old
    checkout(PRODUCT, row)
    assert catalog["catalog_sha256"] == sha(Path("tools/licensecheck/dependencies.yaml").read_bytes())
    assert catalog["policy_sha256"] == sha(Path("governance/licensing.yaml").read_bytes())
    RESULT["preflight_original"] = old
    row["artifact"] = "https://github.com/tamird/gvisor/actions/runs/41525945396/artifacts/13180640411"
    row["archive_sha256"] = sha(path.read_bytes())
    row["verification"] = catalog["verification"]
    row["auditor_source"] = AUDITOR
    row["reused"] = True


def signature_boundary(row: dict) -> None:
    path = TMP / "unsigned-source.zip"
    assert sha(path.read_bytes()) == "9b5ac49e0c24d90ca7d6c486171f06bafe297a6706c900b47ee25e8d9bde31a7"
    with zipfile.ZipFile(path) as z:
        assert z.namelist() == ["result.json"]
        old = json.loads(z.read("result.json"))
    assert old["product"] == PRODUCT and old["carrier"] == "42bcb04e1a6573efc3e34bb8cc72cab4374b4860"
    assert old["status"] == "FAIL" and "runtime_not_run" in old
    assert old["preflight_original"] == RESULT["preflight_original"]
    command = old["phases"]["resolve"]["commands"][0]
    assert command["exit"] == 0 and command["command"][-1] == "@integration_test_packages//:flat"
    assert len(old["phases"]["resolve"]["source_sha256"]) == 3
    RESULT["unsigned_source_original"] = old
    row["artifact"] = "https://github.com/tamird/gvisor/actions/runs/41530612327/artifacts/13181587506"
    row["archive_sha256"] = sha(path.read_bytes())
    row["source_diagnosis"] = "Signed package analysis and supplier hashes passed; the extra key identity reader assumed execution-root symlinks after analysis only."


def resolve(row: dict) -> None:
    assert RESULT["phases"]["prerequisite"]["status"] == "PASS"
    target = "@integration_test_packages//:flat"
    checked("supplier-consumer", BZ + ["cquery", "--config=x86_64", "--output=label", target], row)
    text = checked("supplier-root", BZ + ["info", "output_base"], row)
    roots = [Path(line) for line in text.splitlines() if line.startswith("/")]
    assert len(roots) == 1
    # This is the canonical module repository in the pinned Bazel 8 graph.
    supplier = roots[0] / "external/rules_distroless+"
    row["consumer"] = target
    row["supplier_repository"] = "rules_distroless+"
    expected = {
        "LICENSE": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
        "MODULE.bazel": "96710543b5b35704cab307735d7635ced6c7e37f492aa6720e39eb07a37066d4",
        "apt/private/package_controls.bzl": "14417ed0bf5c80d9e580f8db40ccc26b11ce321b4b513731f272edae21b0b11e",
    }
    actual = {name: sha((supplier / name).read_bytes()) for name in expected}
    assert actual == expected, "Resolved supplier source mismatch"
    row["source_sha256"] = actual


def key_identity(row: dict) -> None:
    assert RESULT["phases"]["resolve"]["status"] == "PASS"
    text = checked("key-root", BZ + ["info", "output_base"], row)
    roots = [Path(line) for line in text.splitlines() if line.startswith("/")]
    assert len(roots) == 1, roots
    text = checked("archive-key", BZ + ["cquery", "--output=files", "@integration_ubuntu_archive_keys//file"], row)
    paths = [Path(line) for line in text.splitlines() if line.startswith("external/")]
    assert len(paths) == 1, paths
    key = roots[0] / paths[0]
    assert key.is_file(), str(key)
    assert sha(key.read_bytes()) == "80a36b0a6de2f69f49d2df75ef473ccde121e9e190b9ea01d20a4f63778d5c31"
    row["signature_key"] = {
        "url": "https://archive.ubuntu.com/ubuntu/project/ubuntu-archive-keyring.gpg",
        "sha256": sha(key.read_bytes()), "size": key.stat().st_size,
        "scope": "qualification-only newer-supplier signed-source prerequisite",
    }
    row["signed_metadata_required"] = True


def inventory(cpu: str, flags: list[str], data: bytes, row: dict) -> None:
    lock_path = one(cpu + "-lock", "@integration_test_packages//:lock.json", flags, row)
    lock = json.loads(lock_path.read_bytes())
    assert lock["version"] == 2
    roots = lock["dependency_sets"]["integration_test_packages"]["sets"][cpu]
    assert len(roots) == 9
    pending = [key + "=" + version for key, version in roots.items()]
    selected = {}
    while pending:
        key = pending.pop()
        if key in selected:
            continue
        pkg = lock["packages"][key]
        assert pkg["architecture"] in (cpu, "all")
        selected[key] = pkg
        pending.extend(dep for dep in pkg["depends_on"]
                       if lock["packages"][dep]["architecture"] in (cpu, "all"))
    expected = sorted((p["name"], p["version"], p["architecture"]) for p in selected.values())
    assert len(expected) == len(set(expected)) == 64
    actual = []
    for paragraph in data.decode().strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in paragraph.splitlines() if ": " in line and not line.startswith(" "))
        assert fields["Status"] == "install ok installed"
        actual.append((fields["Package"], fields["Version"], fields["Architecture"]))
    assert sorted(actual) == expected, {"missing": sorted(set(expected)-set(actual)), "extra": sorted(set(actual)-set(expected)), "count": len(actual)}
    catalog = Path("tools/licensecheck/dependencies.yaml").read_text()
    records = {}
    for name, body in re.findall(r"(?ms)^- dependency: (apt/integration_test_packages/[^\n]+)\n(.*?)(?=^- dependency: |\Z)", catalog):
        fields = dict(re.findall(r"(?m)^  (version|sha256): (.+)$", body))
        assert name not in records
        records[name] = fields
    assert len(records) == 128
    for pkg in selected.values():
        expected_url = lock["sources"][pkg["suite"]]["uris"][0].rstrip("/") + "/" + pkg["filename"]
        record = records["apt/integration_test_packages/" + cpu + "/" + pkg["name"]]
        assert record == {"version": expected_url, "sha256": pkg["sha256"]}
    payloads = outputs(cpu + "-packages", "@integration_test_packages//:packages", flags, row)
    assert len(payloads) == len(set(payloads)) == 64
    row["inventory"] = {"lock_sha256": sha(lock_path.read_bytes()), "status_sha256": sha(data),
                        "tuples": expected, "data_archives": len(payloads), "catalog_records_matched": 64}


def image(arch: str, cpu: str, machine: int, row: dict) -> None:
    assert RESULT["phases"]["prerequisite"]["status"] == "PASS"
    flags = ["--config=" + arch]
    target = "//images/basic:integrationtest_context"
    checked(cpu + "-build", BZ + ["build", *flags, target], row)
    archive = one(cpu + "-path", target, flags, row)
    row["context"] = {"size": archive.stat().st_size, "sha256": sha(archive.read_bytes())}
    with tarfile.open(archive) as tar:
        members = [m for m in tar.getmembers() if m.name.removeprefix("./") == "images/basic/integrationtest/image.tar"]
        assert len(members) == 1 and members[0].isfile()
        saved = PRIVATE / (cpu + "-image.tar")
        with saved.open("wb") as out:
            shutil.copyfileobj(tar.extractfile(members[0]), out)
    checked(cpu + "-load", ["docker", "load", "-i", str(saved)], row)
    info = json.loads(checked(cpu + "-inspect", ["docker", "image", "inspect", TAG], row))[0]
    assert info["Architecture"] == cpu and info["Os"] == "linux"
    assert info["Config"]["User"] == "0:0" and info["Config"]["WorkingDir"] == "/root"
    assert info["Config"]["Cmd"] == ["/bin/bash"] and len(info["RootFS"]["Layers"]) == 3
    row["image"] = {"id": info["Id"], "architecture": cpu, "layers": info["RootFS"]["Layers"]}
    cid = checked(cpu + "-create", ["docker", "create", "--platform=linux/" + cpu, TAG], row).strip()
    assert re.fullmatch(r"[a-f0-9]{64}", cid)
    try:
        rootfs = PRIVATE / (cpu + "-rootfs.tar")
        checked(cpu + "-export", ["docker", "export", "--output=" + str(rootfs), cid], row)
        with tarfile.open(rootfs) as tar:
            files = {m.name.removeprefix("./").rstrip("/"): m for m in tar.getmembers()}
            assert files["tmp"].mode == 0o1777 and files["root"].mode == 0o700
            assert (files["home/nonroot"].uid, files["home/nonroot"].gid) == (1338, 1337)
            assert files["var/run"].issym() and files["var/run"].linkname == "/run"
            for name in ("passwd", "group"):
                assert tar.extractfile(files["etc/" + name]).read() == Path("images/basic/integrationtest/" + name).read_bytes()
            row["helpers"] = {}
            for name in HELPERS:
                member = files["root/" + name]
                data = tar.extractfile(member).read()
                assert member.mode == 0o755 and data[:6] == b"\x7fELF\x02\x01"
                assert int.from_bytes(data[18:20], "little") == machine
                binary = PRIVATE / (cpu + "-" + name)
                binary.write_bytes(data)
                elf = checked(cpu + "-elf-" + name, ["readelf", "-l", "-d", str(binary)], row)
                assert "INTERP" not in elf and "(NEEDED)" not in elf
                row["helpers"][name] = {"size": len(data), "sha256": sha(data), "machine": machine, "static": True}
            inventory(cpu, flags, tar.extractfile(files["var/lib/dpkg/status"]).read(), row)
        command = 'test "$(id -u):$(id -g)" = 1338:1337; test -x ./host_connect; /bin/sh -c true; ip -Version; iptables --version; ip6tables --version; unshare --version; sysctl --version; ping -V; stat /tmp'
        checked(cpu + "-tools", ["docker", "run", "--platform=linux/" + cpu, "--runtime=runc", "--rm", "--workdir=/home/nonroot", "--user=nonroot", TAG, "/bin/bash", "-ec", command], row)
        for fixture in ("test_copy_up", "test_rewinddir"):
            checked(cpu + "-" + fixture, ["docker", "run", "--platform=linux/" + cpu,
                    "--runtime=runc", "--rm", TAG, "./" + fixture], row)
        row["runtime"] = "native AMD64 runc" if cpu == "amd64" else "QEMU ARM64 under AMD64 runc"
    finally:
        subprocess.run(["docker", "rm", "-f", cid], stdout=subprocess.DEVNULL, check=True)


def make_image(row: dict) -> None:
    image_id = RESULT["phases"]["amd64"]["image"]["id"]
    checked("make-image", ["make", "-s", "rebuild-basic_integrationtest", "ARCH=x86_64", "DOCKER_BUILD=false",
                           "STARTUP_OPTIONS=--bazelrc=" + str(TMP / "images.bazelrc")], row)
    actual = checked("make-image-id", ["docker", "image", "inspect", "--format", "{{.Id}}", TAG], row).strip()
    assert actual == image_id
    row["image_id"] = actual


def runtime(row: dict) -> None:
    checked("release-build", BZ + ["build", "--config=x86_64", "//:release"], row)
    path = Path("bazel-bin/release").resolve()
    assert (path / "runsc").is_file() and (path / "gvisor-bin").is_dir()
    row["runsc_sha256"] = sha((path / "runsc").read_bytes())
    checked("install", ["sudo", str(path / "runsc"), "install", "--runtime=integration-c", "--", "--platform=systrap", "--net-raw"], row)
    checked("reload", ["sudo", "systemctl", "reload", "docker"], row)
    runtimes = json.loads(checked("runtime-readback", ["docker", "info", "--format", "{{json .Runtimes}}"], row))
    assert "integration-c" in runtimes


def guest(lane: str, row: dict) -> None:
    assert RESULT["phases"]["make"]["status"] == "PASS"
    if lane == "guest":
        assert RESULT["phases"]["runtime"]["status"] == "PASS"
    p = run(lane, [sys.executable, str(TMP / "guests.py"), lane], row)
    folder = TMP / "results" / lane
    result = json.loads((folder / "result.json").read_text())
    row["consumer"] = result
    for name in ("test.xml", "test.log"):
        row[name + "_sha256"] = sha((folder / name).read_bytes())
    root = ET.fromstring((folder / "test.xml").read_bytes())
    row["cases"] = [{"class": case.get("classname"), "name": case.get("name"),
                     "failed": case.find("failure") is not None or case.find("error") is not None,
                     "skipped": case.find("skipped") is not None} for case in root.iter("testcase")]
    assert row["cases"] and not any(c["failed"] or c["skipped"] for c in row["cases"])
    assert p.returncode == result["rc"] == 0 and result["source"] == PRODUCT
    assert not result["missing"] and not result["FAIL"] and not result["SKIP"]
    assert len(result["required"]) == 6


def phase(name: str, fn: Callable[[dict], None]) -> None:
    row = {"started": now(), "status": "INCOMPLETE"}
    RESULT["phases"][name] = row
    save()
    try:
        fn(row)
        row["status"] = "PASS"
    except Exception as e:
        row["status"] = "FAIL"
        row["error"] = f"{type(e).__name__}: {e}"[:1200]
    finally:
        row["finished"] = now()
        save()


if sys.argv[1:] == ["--preflight"]:
    RESULT["started"] = now()
    phase("original", original)
    phase("catalog", prior_catalog)
    phase("signature_boundary", signature_boundary)
    phase("prerequisite", prerequisite)
    phase("resolve", resolve)
    phase("key_identity", key_identity)
    RESULT["status"] = "PREFLIGHT_PASS" if all(r["status"] == "PASS" for r in RESULT["phases"].values()) else "PREFLIGHT_FAIL"
    RESULT["preflight_finished"] = now()
else:
    assert sys.argv[1:] == ["--runtime"]
    RESULT = json.loads((PUBLIC / "result.json").read_text())
    assert RESULT["product"] == PRODUCT and RESULT["carrier"] == os.environ["GITHUB_SHA"]
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == PRODUCT
    assert sha(Path("MODULE.bazel").read_bytes()) == RESULT["phases"]["prerequisite"]["qualified_module_sha256"]
    if RESULT["phases"]["resolve"]["status"] == "PASS":
        phase("amd64", lambda row: image("x86_64", "amd64", 62, row))
        phase("arm64", lambda row: image("aarch64", "arm64", 183, row))
        phase("make", make_image)
        phase("runtime", runtime)
        phase("nested", lambda row: guest("nested", row))
        phase("guest", lambda row: guest("guest", row))
    else:
        RESULT["runtime_not_run"] = "Supplier repository did not resolve to the pinned source"
    RESULT["status"] = "PASS" if all(row["status"] == "PASS" for row in RESULT["phases"].values()) else "FAIL"
    RESULT["finished"] = now()
save()
print(json.dumps({"status": RESULT["status"], "phases": {k: v["status"] for k, v in RESULT["phases"].items()}}))
raise SystemExit(RESULT["status"] not in ("PASS", "PREFLIGHT_PASS"))
