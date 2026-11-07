"""Compare the stamp, nine installed payloads and container bytes of all six releases."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import tarfile

# This script runs with -I and imports only the source-bound copied helper.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build import CONFIG, D, FORMATS, IMAGE, JOBS, PRODUCT, SOURCE, STAGE, STAMP, save

MEMBERS = {"runsc", "containerd-shim-runsc-v1"} | {"gvisor-bin/" + name for name in (
    "gvisor_sentry", "checkpointgofer", "gvisor-sentry-prewarmer",
    "runsc-fd-parking", "runsc-metric-server", "runsc-netgofer", "gvisor-proc-umounter")}
EXPECTED_ISSUES = (KeyError, ValueError, TypeError, AssertionError, OSError, tarfile.TarError, subprocess.SubprocessError)


def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


@contextmanager
def stream(path):
    if path.name.endswith("bz2"):
        with tarfile.open(path, "r|bz2") as tar:
            yield tar
        return
    args = ["dpkg-deb", "--fsys-tarfile", str(path)] if path.suffix == ".deb" else ["zstd", "-qdc"]
    # zstd's default name handling treats Bazel's .tar.zstd differently from a
    # .tar.zst filename. Supply bytes on stdin as in the prior release check.
    with path.open("rb") as source, subprocess.Popen(args, stdin=source, stdout=subprocess.PIPE,
                                                      stderr=subprocess.DEVNULL) as process:
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as tar:
                yield tar
            while process.stdout.read(1 << 20):
                pass
            assert process.wait() == 0
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()


def deb_control(path, arch, row):
    wanted = {"Package": "runsc", "Version": STAMP.removeprefix("release-"), "Architecture": arch}
    for k, v in wanted.items():
        actual = subprocess.check_output(["dpkg-deb", "--field", path, k], text=True).strip()
        row[k] = actual == v
    with subprocess.Popen(["dpkg-deb", "--ctrl-tarfile", path],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for entry in tar:
                name = entry.name.removeprefix("./")
                if name not in ("changelog", "postinst", "conffiles"):
                    continue
                assert entry.isreg() and entry.size < 65536
                data = tar.extractfile(entry).read()
                if name == "changelog":
                    lines = data.decode().splitlines()
                    row[name] = lines[0] == "runsc (20261106.0) unstable; urgency=medium" and lines[-1].endswith("Fri, 06 Nov 2026 00:00:00 +0000")
                elif name == "postinst":
                    row[name] = entry.mode == 0o755 and data == Path("debian/postinst.sh").read_bytes()
                else:
                    row[name] = data.strip() == b"/etc/containerd/runsc.toml"
        proc.stdout.read()
        assert proc.wait() == 0
    return set(row) == set(wanted) | {"changelog", "postinst", "conffiles"} and all(row.values())


def archive(path, arch, row):
    row.update(bytes=path.stat().st_size, sha256=sha(path), files={})
    deb = path.suffix == ".deb"
    machine = {"amd64": 62, "arm64": 183}[arch]
    save_cli = deb and arch == "amd64" and platform.machine() == "x86_64"
    with stream(path) as tar:
        for entry in tar:
            name = entry.name.removeprefix("./")
            if entry.isdir():
                continue
            if deb and name == "etc/containerd/runsc.toml":
                row["config"] = (entry.isreg() and entry.mode == 0o644 and entry.size < 65536
                                 and tar.extractfile(entry).read() == Path("shim/runsc.toml").read_bytes())
                continue
            if deb and not name.startswith("usr/bin/"):
                raise ValueError("unexpected deb payload")
            if deb:
                name = name.removeprefix("usr/bin/")
            assert name in MEMBERS and name not in row["files"] and entry.isreg()
            assert 0 < entry.size < 300 << 20 and entry.mode & 0o7777 == 0o755
            with tar.extractfile(entry) as f:
                header = f.read(64)
                assert len(header) == 64 and header[:7] == b"\x7fELF\x02\x01\x01"
                em = int.from_bytes(header[18:20], "little")
                assert em == machine
                digest = hashlib.sha256(header)
                tail = header
                has_label = False
                cli = path.parent / "version-cli" if save_cli and name == "runsc" else None
                copy = cli.open("wb") if cli else None
                try:
                    if copy:
                        copy.write(header)
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        digest.update(chunk)
                        if name in ("runsc", "gvisor-bin/gvisor_sentry") and not has_label:
                            has_label = STAMP.encode() in tail + chunk
                            tail = chunk[-len(STAMP):]
                        if copy:
                            copy.write(chunk)
                finally:
                    if copy:
                        copy.close()
                if cli:
                    cli.chmod(0o700)
                    try:
                        stdout = subprocess.check_output([cli, "--version"], text=True, timeout=15)
                        row["native_cli_stamp"] = stdout.splitlines()[0] == "runsc version " + STAMP
                    finally:
                        cli.unlink()
                if name in ("runsc", "gvisor-bin/gvisor_sentry"):
                    row.setdefault("linked_label", {})[name] = has_label
                row["files"][name] = {"sha256": digest.hexdigest(), "bytes": entry.size,
                                      "mode": entry.mode & 0o7777, "machine": em}
    control_ok = True
    if deb:
        row["control"] = {}
        control_ok = deb_control(path, arch, row["control"]) and row.get("config") is True
    return (set(row["files"]) == MEMBERS and all(row["linked_label"].values()) and control_ok
            and (not save_cli or row.get("native_cli_stamp") is True))


def workers(key, plan, packages):
    # Join each recorded spawn to its emitted archive, its configured output
    # and the independently saved bytes from the declaring public target.
    query = r'''if .file and (.file.path | test("/bin/debian/(runsc[.]deb|gvisor[.]tar[.](bz2|zstd))$")) then
        ["file",.id,.file.path,.file.digest.hash,(.file.digest.sizeBytes//0)]
      elif .spawn then .spawn | .targetLabel |= ((. // "") | sub("^@@";"")) |
        select($wanted[.targetLabel] == .mnemonic) |
        ["spawn",.targetLabel,.mnemonic,(.runner=="remote"),(.cacheHit//false),(.exitCode//0),
         ((.platform.properties//[]) | map({key:.name,value:.value}) | from_entries |
           [(.Arch // "unspecified"),(."container-image"==$image and ."workload-isolation-type"=="oci")]),
         [.outputs[]? | .outputId]]
      else empty end'''
    wanted = dict(FORMATS.values())
    log = D / (key + ".compact")
    with subprocess.Popen([D / "bb", "print", "--raw", "--compact_execution_log=" + str(log)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as decoder:
        data = subprocess.check_output(["jq", "-c", "--argjson", "wanted", json.dumps(wanted),
                                        "--arg", "image", IMAGE, query], stdin=decoder.stdout, text=True)
        decoder.stdout.close()
        assert decoder.wait() == 0
    files = {}
    spawns = []
    for line in data.splitlines():
        record = json.loads(line)
        if record[0] == "file":
            _, ident, path, digest, size = record
            files[str(ident)] = (path, digest, int(size))
        else:
            spawns.append(record)
    rows = []
    for _, owner, mnemonic, remote, cached, code, (raw_arch, image), ids in spawns:
        arch = raw_arch if raw_arch in CONFIG else "other"
        filename = next(n for n, pair in FORMATS.items() if pair == (owner, mnemonic))
        matches = [a for a in plan.get("actions", []) if a["owner"] == owner and a["target"] == arch]
        found = [files[str(i)] for i in ids if str(i) in files and files[str(i)][0].endswith("/" + filename)]
        row = {"owner": owner, "mnemonic": mnemonic, "worker": arch, "remote": remote,
               "cached": cached, "exit": code, "image": image, "verified": False}
        if len(matches) == len(found) == 1:
            expected, (path, digest, size) = matches[0], found[0]
            materialized = packages.get(arch, {}).get(filename, {})
            mapped = "bazel-out/cfg/bin/debian/" + filename
            row["path_form"] = "declared" if path == expected["output"] else "mapped" if path == mapped else "other"
            row["configuration"] = expected["configuration"]
            row["output_sha256"] = digest if re.fullmatch(r"[0-9a-f]{64}", digest or "") else None
            row["output_bytes"] = size
            row["matches_package"] = (row["output_sha256"] is not None and digest == materialized.get("sha256")
                                      and size == materialized.get("bytes"))
            row["verified"] = (row["matches_package"] and row["path_form"] in ("declared", "mapped")
                               and expected["worker"] == arch and remote and not cached and code == 0 and image)
        rows.append(row)
    expected = [(label, a) for label, _ in FORMATS.values() for a in (CONFIG if key == "stage" else [key])]
    return {"rows": rows, "verified": (sorted((r["owner"], r["worker"]) for r in rows) == sorted(expected)
                                        and all(r["verified"] for r in rows))}


def main():
    assert not (D / "client.rc").exists()
    result = {"source": SOURCE, "product": PRODUCT, "stamp": STAMP, "image": IMAGE,
              "runs": {}, "comparison": {}, "verified": False}
    save("result", result)
    for key in JOBS:
        row = result["runs"][key] = {"packages": {}}
        for field in ("plan", "build"):
            p = D / (field + "-" + key + ".json")
            if p.is_file():
                row[field] = json.loads(p.read_bytes())
        for arch in CONFIG if key == "stage" else [key]:
            packages = row["packages"][arch] = {}
            for filename, (owner, _) in FORMATS.items():
                pkg = packages[filename] = {}
                p = D / "files" / key / arch / filename
                if p.is_file():
                    try:
                        pkg["verified"] = archive(p, arch, pkg)
                    except EXPECTED_ISSUES as e:
                        pkg["error"] = type(e).__name__
                t = STAGE if key == "stage" else owner
                b = row.get("build", {}).get("bep", {}).get("completed", {}).get(t, {})
                pkg["declared"] = b.get("built") is True and arch + "/" + filename in b.get("declarations", [])
                save("result", result)
        try:
            row["workers"] = workers(key, row.get("plan", {}), row["packages"])
        except EXPECTED_ISSUES as e:
            row["workers"] = {"error": type(e).__name__}
        build = row.get("build", {})
        row["verified"] = (row.get("plan", {}).get("verified") is True and build.get("exit") == 0 and
                           build.get("bep", {}).get("exit") == 0 and row["workers"].get("verified") is True and
                           all(pkg.get("verified") and pkg.get("declared") for packages in row["packages"].values()
                               for pkg in packages.values()))
        save("result", result)
    for arch in CONFIG:
        table = result["comparison"][arch] = {}
        staged = result["runs"]["stage"]["packages"][arch]
        direct = result["runs"][arch]["packages"][arch]
        reference = staged.get("gvisor.tar.bz2", {}).get("files", {})
        for filename in FORMATS:
            a, b = staged[filename], direct[filename]
            pairs = [a.get("files", {}), b.get("files", {})]
            matched = sorted(n for n in MEMBERS if n in pairs[0] and pairs[0].get(n) == pairs[1].get(n))
            table[filename] = {
                "same_archive": bool(a.get("sha256") and a["sha256"] == b.get("sha256")),
                "matching_executables": matched,
                "same_payload_across_formats": bool(reference and all(files == reference for files in pairs)),
            }
    result["verified"] = (all(row["verified"] for row in result["runs"].values()) and
        all(item["same_archive"] and item["same_payload_across_formats"] and set(item["matching_executables"]) == MEMBERS
            for formats in result["comparison"].values() for item in formats.values()))
    save("result", result)



if __name__ == "__main__":
    main()
