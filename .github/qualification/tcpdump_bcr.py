"""Check the Make archive, signed package closure and dpkg database independently."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tarfile
from urllib.request import urlopen


WORK = Path(os.environ["RUNNER_TEMP"]) / "tcpdump-bcr"
OUT = WORK / os.environ.get("CPU", "amd64")
CPU = os.environ.get("CPU", "amd64")
ARCH = {"amd64": "x86_64", "arm64": "aarch64"}[CPU]
REGISTRY = "https://raw.githubusercontent.com/bazel-contrib/bazel-central-registry/0fcf9560a28476c948c16a6b11fca730cb76f4ec"
SNAPSHOT = "https://snapshot.ubuntu.com/ubuntu/20260928T000000Z/"
MODULE = "/modules/rules_distroless/0.11.1/"
KEY = "80a36b0a6de2f69f49d2df75ef473ccde121e9e190b9ea01d20a4f63778d5c31"
RELEASE = "418d996ef937d7e069610830876981cfba1c72245f9b32d55ce20b6b5e600b43"
TAG = "gvisor.dev/images/basic/tcpdump:latest"
PIN = {
    "amd64": (62, "99d9b69491ed37afd3d3ce2fad50637a6659a12a2ac6c7df8ffc13c1bc9cc871"),
    "arm64": (183, "255750051295662c82920267c9b3c072090b7fb8f23a7dc87c1c9bc8ac2693a4"),
}
BZ = ["bazelisk", "--bazelrc=" + str(WORK / "images.bazelrc")]


def write(name, data):
    (WORK / "results" / (name + ".json")).write_text(json.dumps(data, indent=2) + "\n")


def checked(args):
    return subprocess.run(args, stdout=subprocess.PIPE, text=True, check=True).stdout.strip()


def digest(stream):
    h = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        h.update(chunk)
    return h.hexdigest()


def sha(path):
    with path.open("rb") as stream:
        return digest(stream)


def output(label):
    paths = checked([*BZ, "cquery", "--config=" + ARCH, "--output=files", label]).splitlines()
    one, = paths
    p = Path(one)
    if p.is_file():
        return p
    prefix = "output_base" if p.parts[0] == "external" else "execution_root"
    return Path(checked([*BZ, "info", prefix])) / p


def registry():
    source_url = REGISTRY + MODULE + "source.json"
    metadata = urlopen(source_url, timeout=40).read(65536)
    assert hashlib.sha256(metadata).hexdigest() == "a85839b163e17c4e4f3d44766de14bd69c40e4c1dec345aff98a3258b630c92f"
    data = json.loads(metadata)
    assert data["integrity"] == "sha256-" + base64.b64encode(bytes.fromhex(RELEASE)).decode()
    assert data["url"] == "https://github.com/bazel-contrib/rules_distroless/releases/download/v0.11.1/rules_distroless-v0.11.1.tar.gz"
    with urlopen(data["url"], timeout=60) as archive:
        actual = digest(archive)
    assert actual == RELEASE
    patches = data["patches"]
    assert set(patches) == {"module_dot_bazel_version.patch"}
    patch_data = urlopen(REGISTRY + MODULE + "patches/module_dot_bazel_version.patch", timeout=30).read(8192)
    assert patches["module_dot_bazel_version.patch"] == "sha256-" + base64.b64encode(hashlib.sha256(patch_data).digest()).decode()
    write("registry", {"source": os.environ["PRODUCT"], "candidate": REGISTRY,
                       "source_json_sha256": hashlib.sha256(metadata).hexdigest(),
                       "upstream_release_sha256": actual, "registry_patches": patches})


def apt_packages(package_lock):
    roots = package_lock["dependency_sets"]["tcpdump_packages"]["sets"][CPU]
    packages = package_lock["packages"]
    assert {packages[f"{k}={v}"]["name"] for k, v in roots.items()} == {"tcpdump", "base-files"}
    pending = [f"{key}={v}" for key, v in roots.items()]
    selected = {}
    while pending:
        pkg_key = pending.pop()
        if pkg_key not in selected:
            pkg = packages[pkg_key]
            assert pkg["architecture"] in (CPU, "all")
            selected[pkg_key] = pkg
            pending.extend(dep for dep in pkg["depends_on"] if packages[dep]["architecture"] in (CPU, "all"))
    return list(selected.values())


def package_file(pkg, cache):
    checksum = pkg["sha256"]
    assert re.fullmatch(r"[a-f0-9]{64}", checksum)
    path = cache / "content_addressable/sha256" / checksum / "file"
    if not path.is_file():
        path = WORK / "debs" / checksum
        if not path.is_file():
            url, *_ = pkg["urls"]
            assert url.startswith(SNAPSHOT + "pool/")
            with urlopen(url, timeout=60) as response, path.open("wb") as out:
                shutil.copyfileobj(response, out)
    assert sha(path) == checksum
    return path


def payloads(pkg, path, flat_hashes):
    # Compare the original data member of each signed .deb to the Make image's
    # installed files. dpkg status below is independent of this measurement.
    command = subprocess.Popen(["dpkg-deb", "--fsys-tarfile", str(path)], stdout=subprocess.PIPE)
    matched = missing = changed = 0
    examples = []
    tcpdump_payload = {}
    with tarfile.open(fileobj=command.stdout, mode="r|") as contents:
        for member in contents:
            if not member.isfile():
                continue
            name = member.name.removeprefix("./").lstrip("/")
            expected = digest(contents.extractfile(member))
            actual = flat_hashes.get(name)
            if name == "usr/bin/tcpdump":
                tcpdump_payload = {"signed_binary_sha256": expected, "installed_binary_sha256": actual}
            if actual == expected:
                matched += 1
            else:
                missing += actual is None
                changed += actual is not None
                if len(examples) < 3:
                    examples.append(name)
    with command.stdout as stream:
        while stream.read(65536):
            pass
    assert command.wait() == 0
    if pkg["name"] == "tcpdump":
        assert tcpdump_payload and tcpdump_payload["signed_binary_sha256"] == tcpdump_payload["installed_binary_sha256"]
    return {**tcpdump_payload, "name": pkg["name"], "version": pkg["version"], "architecture": pkg["architecture"],
            "deb_sha256": pkg["sha256"], "regular_files_match": matched, "missing": missing,
            "different": changed, "first_differences": examples}


def image():
    row = {"source": os.environ["PRODUCT"], "cpu": CPU, "verified_image": False}
    write(CPU, row)
    made = OUT / "contexts.tar"
    row["make_context_sha256"] = sha(made)
    row["bazel_context_sha256"] = sha(output("//images/basic:tcpdump_context"))
    assert row["make_context_sha256"] == row["bazel_context_sha256"]
    archive = OUT / "image.tar"
    with tarfile.open(made) as ctx, archive.open("wb") as image_file:
        members = [m for m in ctx if m.isfile()]
        assert [m.name.removeprefix("./") for m in members] == ["images/basic/tcpdump/image.tar"]
        shutil.copyfileobj(ctx.extractfile(members[0]), image_file)
    row["docker_tar_sha256"] = sha(archive)
    with tarfile.open(archive) as tar:
        item, = json.load(tar.extractfile("manifest.json"))
        config_data = tar.extractfile(item["Config"]).read()
        config = json.loads(config_data)
        assert config["architecture"] == CPU and config["os"] == "linux"
        assert TAG in item["RepoTags"]
        row["docker_image_id"] = "sha256:" + hashlib.sha256(config_data).hexdigest()
        hashes = {}
        contents = {}
        for layer in item["Layers"]:
            with tarfile.open(fileobj=tar.extractfile(layer), mode="r|*") as files:
                for member in files:
                    if not member.isfile():
                        continue
                    name = member.name.removeprefix("./").lstrip("/")
                    stream = files.extractfile(member)
                    if name in {"var/lib/dpkg/status", "usr/bin/tcpdump", "etc/group", "etc/passwd", "usr/bin/tcpdump_sender"}:
                        data = stream.read()
                        contents[name] = data
                        hashes[name] = hashlib.sha256(data).hexdigest()
                    else:
                        hashes[name] = digest(stream)
    row["runtime_files"] = {name: hashes[name] for name in ("usr/bin/tcpdump", "usr/bin/tcpdump_sender", "etc/passwd", "etc/group", "usr/share/doc/tcpdump/copyright")}
    for name in ("usr/bin/tcpdump", "usr/bin/tcpdump_sender"):
        assert contents[name][:6] == b"\x7fELF\x02\x01" and contents[name][18:20] == struct.pack("<H", PIN[CPU][0])
    assert b"tcpdump:x:128:128:" in contents["etc/passwd"] and b"tcpdump:x:128:" in contents["etc/group"]
    write(CPU, row)

    archive_key_sha256 = sha(output("@image_ubuntu_archive_keys//file:ubuntu-archive-keyring.gpg"))
    assert archive_key_sha256 == KEY
    row["archive_key_sha256"] = archive_key_sha256
    path = output("@tcpdump_packages//:lock.json")
    row["package_lock_sha256"] = sha(path)
    lock = json.loads(path.read_text())
    assert lock["version"] == 2
    pkgs = apt_packages(lock)
    root_pkg, = (p for p in pkgs if p["name"] == "tcpdump")
    assert root_pkg["version"] == "4.99.1-3ubuntu0.2" and root_pkg["sha256"] == PIN[CPU][1]
    record = contents["var/lib/dpkg/status"].decode()
    installed = set()
    for paragraph in record.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in paragraph.splitlines() if ": " in line and not line.startswith(" "))
        assert fields["Status"] == "install ok installed"
        installed.add((fields["Package"], fields["Version"], fields["Architecture"]))
    expected = {(p["name"], p["version"], p["architecture"]) for p in pkgs}
    row.update(signed_lock_packages=sorted(expected), dpkg_status=sorted(installed),
               absent_from_status=sorted(expected - installed), extra_in_status=sorted(installed - expected),
               closure_payloads=[])
    write(CPU, row)
    cache = Path(checked([*BZ, "info", "repository_cache"]))
    for pkg in sorted(pkgs, key=lambda p: p["name"]):
        row["closure_payloads"].append(payloads(pkg, package_file(pkg, cache), hashes))
        write(CPU, row)
    row["all_payloads_match"] = all(p["regular_files_match"] > 0 and p["missing"] == p["different"] == 0 for p in row["closure_payloads"])
    row["verified_image"] = bool(len(row["closure_payloads"]) == len(expected) and not row["extra_in_status"]
                                 and all(p["regular_files_match"] > 0 for p in row["closure_payloads"]))
    if CPU == "amd64":
        # A real executable and libpcap introspection on this image. The
        # packet captures using the corrected Sentry have separate receipts.
        checked(["docker", "load", "--input", str(archive)])
        assert checked(["docker", "inspect", "--type", "image", "--format", "{{.Id}}", TAG]) == row["docker_image_id"]
        version = subprocess.run(["docker", "run", "--rm", "--runtime=runc", TAG, "tcpdump", "--version"], text=True, capture_output=True)
        dlt = subprocess.run(["docker", "run", "--rm", "--runtime=runc", TAG, "tcpdump", "-i", "any", "-L"], text=True, capture_output=True)
        row["native_runc"] = {"version_exit": version.returncode, "libpcap_1_10_1": "libpcap version 1.10.1" in version.stdout + version.stderr,
                              "tcpdump_4_99_1": "tcpdump version 4.99.1" in version.stdout + version.stderr,
                              "list_exit": dlt.returncode, "advertised_sll2": "LINUX_SLL2" in dlt.stdout + dlt.stderr}
    write(CPU, row)


def collect():
    results = WORK / "results"
    lock = Path("MODULE.bazel.lock")
    lockfiles = json.loads(lock.read_text()).get("registryFileHashes", {}) if lock.is_file() else {}
    relevant = {key: value for key, value in lockfiles.items() if key.startswith(REGISTRY + MODULE)}
    license_log = WORK / "license.log"
    text = license_log.read_text(errors="replace") if license_log.is_file() else ""
    missing = sorted(set(re.findall(r"missing entry for ([\w.@+/:~-]+)", text)))
    problems = [line.strip()[:360] for line in text.splitlines() if re.search(r" was audited | license_source | has no license|unrecognized license|not in dependency list", line)][:20]
    stages = {}
    for exit_file in WORK.glob("*.exit"):
        stages[exit_file.stem] = int(exit_file.read_text())
    remarks = {}
    for log in WORK.glob("*.log"):
        with log.open(errors="replace") as stream:
            remarks[log.stem] = [line.rstrip()[:410] for line in stream if re.search(r"(^ERROR:|no such package|Traceback|AssertionError|HTTP Error|Repository.*failed|cannot run make mod|^make.*Error|NAMESPACE|undefined reference)", line)][:12]
    row = {"source": os.environ["PRODUCT"], "exits": stages, "license_missing": missing,
           "other_catalog_problems": problems, "module_registry_lock": relevant, "diagnostics": remarks}
    row["candidate_selected"] = relevant.get(REGISTRY + MODULE + "source.json") == "a85839b163e17c4e4f3d44766de14bd69c40e4c1dec345aff98a3258b630c92f"
    for cpu in PIN:
        p = results / (cpu + ".json")
        row[cpu] = json.loads(p.read_text()) if p.is_file() else {}
    row["complete"] = bool((results / "registry.json").is_file() and row["candidate_selected"]
                           and stages.get("license") is not None and (stages["license"] == 0 or missing or problems)
                           and all(row[c].get("verified_image") for c in PIN)
                           and all(stages.get("image-" + c) == 0 for c in PIN)
                           and row["amd64"].get("native_runc", {}).get("list_exit") == 0
                           and row["amd64"]["native_runc"].get("advertised_sll2"))
    write("comparison", row)


if __name__ == "__main__":
    {"registry": registry, "image": image, "collect": collect}[sys.argv[1]]()
