"""Check the signed package closure, Docker image, and native workload outputs."""

from datetime import datetime, timezone
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
from xml.etree import ElementTree

TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "transfer"
CPU = os.environ["CPU"]
TAG = "gvisor.dev/images/basic/tcpdump:latest"
PIN = {
    "amd64": (62, "99d9b69491ed37afd3d3ce2fad50637a6659a12a2ac6c7df8ffc13c1bc9cc871"),
    "arm64": (183, "255750051295662c82920267c9b3c072090b7fb8f23a7dc87c1c9bc8ac2693a4"),
}
BZ = ["bazel", "--bazelrc=" + str(TMP / "images.bazelrc")]


def checked(args: list[str]) -> str:
    return subprocess.run(args, stdout=subprocess.PIPE, text=True, check=True).stdout.strip()


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def output(label: str) -> Path:
    paths = checked([*BZ, "cquery", "--config=" + os.environ["ARCH"], "--output=files", label]).splitlines()
    match paths:
        case [one]:
            p = Path(one)
        case _:
            raise ValueError(f"{label}: unexpected outputs {paths}")
    if p.is_file():
        return p
    prefix = "output_base" if p.parts[0] == "external" else "execution_root"
    return Path(checked([*BZ, "info", prefix])) / p


def export_image() -> None:
    OUT.mkdir(exist_ok=True)
    key = sha(output("@image_ubuntu_archive_keys//file:ubuntu-archive-keyring.gpg"))
    assert key == "80a36b0a6de2f69f49d2df75ef473ccde121e9e190b9ea01d20a4f63778d5c31", key
    lock = json.loads(output("@tcpdump_packages//:lock.json").read_text())
    assert lock["version"] == 2, lock["version"]
    roots = lock["dependency_sets"]["tcpdump_packages"]["sets"][CPU]
    packages = lock["packages"]
    assert {packages[f"{k}={v}"]["name"] for k, v in roots.items()} == {"tcpdump", "base-files"}, roots
    pending = [f"{k}={v}" for k, v in roots.items()]
    selected = {}
    while pending:
        key = pending.pop()
        if key not in selected:
            pkg = packages[key]
            assert pkg["architecture"] in (CPU, "all"), pkg
            selected[key] = pkg
            pending.extend(dep for dep in pkg["depends_on"] if packages[dep]["architecture"] in (CPU, "all"))
    expected = {(p["name"], p["version"], p["architecture"]) for p in selected.values()}
    tcpdump, = (p for p in selected.values() if p["name"] == "tcpdump")
    assert tcpdump["version"] == "4.99.1-3ubuntu0.2" and tcpdump["sha256"] == PIN[CPU][1], tcpdump
    with tarfile.open(output("@tcpdump_packages//:flat")) as flat:
        names = {m.name.removeprefix("./"): m for m in flat}
        assert not {"etc/group", "etc/passwd"}.intersection(names), "overlay would discard packaged identities"
        record = flat.extractfile(names["var/lib/dpkg/status"]).read().decode()
        installed = set()
        for paragraph in record.strip().split("\n\n"):
            fields = dict(line.split(": ", 1) for line in paragraph.splitlines() if ": " in line and not line.startswith(" "))
            assert fields["Status"] == "install ok installed", fields
            installed.add((fields["Package"], fields["Version"], fields["Architecture"]))
        assert installed == expected, (expected - installed, installed - expected)
        copyright_text = flat.extractfile(names["usr/share/doc/tcpdump/copyright"]).read()
        assert b"copyright" in copyright_text.lower(), "no tcpdump license text"
        assert flat.extractfile(names["usr/bin/tcpdump"]).read(20)[18:20] == struct.pack("<H", PIN[CPU][0]), CPU
    archive = OUT / "image.tar"
    with tarfile.open(output("//images/basic:tcpdump_context")) as ctx, archive.open("wb") as f:
        members = [m for m in ctx if m.isfile()]
        assert [m.name.removeprefix("./") for m in members] == ["images/basic/tcpdump/image.tar"], members
        shutil.copyfileobj(ctx.extractfile(members[0]), f)
    with tarfile.open(archive) as tar:
        item, = json.load(tar.extractfile("manifest.json"))
        config_data = tar.extractfile(item["Config"]).read()
        config = json.loads(config_data)
        assert config["architecture"] == CPU and config["os"] == "linux", config["architecture"]
        assert TAG in item["RepoTags"], item["RepoTags"]
    (OUT / "inventory.json").write_text(json.dumps({
        "source": os.environ["PRODUCT"], "cpu": CPU, "archive_sha256": sha(archive),
        "docker_image_id": "sha256:" + hashlib.sha256(config_data).hexdigest(),
        "archive_key_sha256": key, "tcpdump_sha256": tcpdump["sha256"], "tcpdump_copyright_sha256": hashlib.sha256(copyright_text).hexdigest(),
        "packages": sorted(expected), "status_sha256": hashlib.sha256(record.encode()).hexdigest(),
    }, indent=2) + "\n")


def runtime() -> None:
    i = json.loads((OUT / "inventory.json").read_text())
    assert (i["source"], i["cpu"], i["archive_sha256"]) == (os.environ["PRODUCT"], CPU, sha(OUT / "image.tar"))
    assert checked(["docker", "image", "inspect", "--format", "{{.Architecture}} {{.Id}}", TAG]) == CPU + " " + i["docker_image_id"]
    ids = {}
    for line in (TMP / "tcpdump-ids").read_text().splitlines():
        if m := re.fullmatch(r"(Uid|Gid):\s+(.+)", line):
            ids[m[1]] = m[2].split()
    assert ids == {"Uid": ["128"] * 4, "Gid": ["128"] * 4}, ids
    logs = (TMP / "privilege-capture.log").read_text()
    for size in (4, 8):
        assert f"127.0.0.1.9999 > 127.0.0.1.9999: UDP, length {size}" in logs, logs[-2000:]
    root = ElementTree.parse("bazel-testlogs/test/image/image_test/test.xml")
    cases = {case.attrib["name"]: [child.tag for child in case] for case in root.iter("testcase")}
    for required in ("TestTcpdump/lo", "TestTcpdump/any"):
        assert required in cases and not {"error", "failure", "skipped"}.intersection(cases[required]), cases
    print(json.dumps({"source": i["source"], "cpu": CPU, "image": i["docker_image_id"], "uid_gid": ids, "cases": cases, "execution_finished": os.environ["FINISHED"], "verdict_at": datetime.now(timezone.utc).isoformat()}))


if __name__ == "__main__":
    if sys.argv[1] == "inventory":
        export_image()
    elif sys.argv[1] == "runtime":
        runtime()
    else:
        raise ValueError(sys.argv)
