import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tarfile


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def entries(tar, expected_machine):
    rows = {}
    for member in tar:
        if not member.isfile():
            continue
        name = member.name[2:] if member.name.startswith("./") else member.name
        source = tar.extractfile(member)
        first = source.read(64)
        if first[:4] != b"\x7fELF":
            continue
        assert len(first) == 64 and first[4:6] == b"\x02\x01"
        machine = struct.unpack_from("<H", first, 18)[0]
        assert machine == expected_machine, (name, machine)
        h = hashlib.sha256(first)
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
        assert name not in rows, name
        rows[name] = [member.size, h.hexdigest()]
    return rows


def main():
    root, label, out = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    result = {}
    for arch, cpu, machine in (("x86_64", "amd64", 62), ("aarch64", "arm64", 183)):
        paths = {name: root / arch / name for name in ("runsc.deb", "gvisor.tar.bz2", "gvisor.tar.zstd")}
        deb = paths["runsc.deb"]
        def field(key):
            return subprocess.check_output(["dpkg-deb", "-f", str(deb), key], text=True).strip()
        assert (field("Package"), field("Architecture"), field("Version")) == ("runsc", cpu, label[len("release-"):])
        with tarfile.open(paths["gvisor.tar.bz2"], "r:bz2") as stream:
            archive = entries(stream, machine)
        with subprocess.Popen(["zstd", "-dc", str(paths["gvisor.tar.zstd"])], stdout=subprocess.PIPE) as process:
            with tarfile.open(fileobj=process.stdout, mode="r|") as stream:
                zstd = entries(stream, machine)
            assert process.wait() == 0
        with subprocess.Popen(["dpkg-deb", "--fsys-tarfile", str(deb)], stdout=subprocess.PIPE) as process:
            with tarfile.open(fileobj=process.stdout, mode="r|") as stream:
                installed = entries(stream, machine)
            assert process.wait() == 0
        assert archive == zstd and "runsc" in archive and "gvisor-bin/gvisor_sentry" in archive
        assert all(installed.get("usr/bin/"+n) == v for n, v in archive.items())
        result[arch] = {"version": field("Version"), "control_arch": cpu, "elf_machine": machine,
                        "outputs": {name:[p.stat().st_size,sha(p)] for name,p in paths.items()}, "binaries":archive}
    out.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")


if __name__ == "__main__":
    main()
