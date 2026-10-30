"""Verify the checker, compare real release layouts, and retain only receipts."""
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def inspect(path):
    result = {}
    cmd = ["zstd", "-dc", str(path)] if path.suffix == ".zstd" else ["bzip2", "-dc", str(path)]
    with subprocess.Popen(cmd, stdout=subprocess.PIPE) as reader:
        with tarfile.open(fileobj=reader.stdout, mode="r|") as archive:
            for member in archive:
                name = member.name.removeprefix("./").removesuffix("/")
                if not name or name == ".":
                    continue
                require(name not in result, f"duplicate archive path {name}")
                digest = None
                if member.isfile():
                    digest = hashlib.file_digest(archive.extractfile(member), "sha256").hexdigest()
                result[name] = dict(mode=member.mode, type=member.type.decode(),
                                    size=member.size, link=member.linkname, sha256=digest,
                                    uid=member.uid, gid=member.gid, uname=member.uname, gname=member.gname,
                                    mtime=member.mtime)
        while reader.stdout.read(1024 * 1024):
            pass
        require(reader.wait() == 0, f"failed to decompress {path}")
    return result


def tiny(archive, names):
    with tarfile.open(archive, mode="w:gz") as staged:
        for name in names:
            item = tarfile.TarInfo("./" + name)
            item.size = 1
            staged.addfile(item, io.BytesIO(b"x"))


def run_checker(binary, path):
    with path.open("rb") as staged:
        proc = subprocess.run([binary], stdin=staged, capture_output=True, text=True)
    return proc.returncode, proc.stderr.strip()


def main():
    mode, out = sys.argv[1:3]
    out = Path(out)
    if mode == "check":
        binary, manifest, actions, executed = sys.argv[3:]
        raw=json.loads(Path(actions).read_text())
        forbidden = {"PackageTar", "PackageDeb", "ReleaseFiles"}
        live = [a.get("mnemonic") for a in raw.get("actions", [])]
        require("GoLink" in live and not forbidden.intersection(live), str(live))
        require(not any("/pkg/sentry" in a for t in raw.get("targets", []) for a in [t.get("label","")]), "checker depends on the sentry")
        # The configured graph includes the package's sources; the name-
        # manifest FileWrite action needs no binary as an input. Check the
        # spawns actually requested, not merely the owners analyzed.
        text = Path(executed).read_text().strip()
        spawns = []
        decoder = json.JSONDecoder()
        while text:
            value, used = decoder.raw_decode(text)
            spawns.extend(value if isinstance(value, list) else [value])
            text = text[used:].lstrip()
        actions = [str(row.get("mnemonic", "")) for row in spawns]
        require("GoLink" in actions and not forbidden.intersection(actions), str(set(actions)))
        require(not any("pkg/sentry/" in str(row.get("targetLabel",row.get("target_label",""))) or
                        "//runsc:" in str(row.get("targetLabel",row.get("target_label",""))) for row in spawns),
                "actual checker build ran a sentry or runsc action")
        names = Path(manifest).read_text().splitlines()
        require("runsc" in names and "containerd-shim-runsc-v1" in names and
                len([name for name in names if name.startswith("gvisor-bin/")]) > 1, str(names))
        receipt = dict(names=names, direct_actions=sorted(set(live)), executed_actions=sorted(set(actions)), spawns=len(spawns), cases={})
        for case in ("complete", "missing", "corrupt"):
            path = out.parent / f"staged-{case}.tar.gz"
            if case == "missing":
                missing = next(s for s in names if s.startswith("gvisor-bin/"))
                tiny(path, [s for s in names if s != missing])
            else:
                tiny(path, names + ["extra/payload"])
                if case == "corrupt":
                    data = bytearray(path.read_bytes())
                    data[-8] ^= 1
                    path.write_bytes(data)
            code, error = run_checker(binary, path)
            if case == "complete":
                require(code == 0, error)
            else:
                require(code != 0 and (missing in error if case == "missing" else "checksum" in error), error)
            receipt["cases"][case] = dict(exit=code, diagnostic=error)
        (out/"checker.json").write_text(json.dumps(receipt, indent=2) + "\n")
    elif mode == "archive":
        for path in sys.argv[3:]:
            p = Path(path)
            d = inspect(p)
            (out/f"{p.name}.json").write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")
    elif mode == "compare":
        receipts = sorted(out.glob("gvisor.*.json"))
        require(len(receipts) == 4, str(receipts))
        captures = [json.loads(p.read_text()) for p in receipts]
        require(all(c == captures[0] for c in captures), "archive metadata or payload differs; inspect retained receipts")
        expected = json.loads((out/"checker.json").read_text())["names"]
        files = sorted(n for n, m in captures[0].items() if m["type"] != "5")
        require(files == expected, f"{files} != {expected}")
        print(f"{len(files)} binaries match by name, mode, content and link type across all four archives")
    else:
        raise ValueError(mode)


if __name__ == "__main__":
    main()
