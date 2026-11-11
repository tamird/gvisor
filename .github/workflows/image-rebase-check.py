"""Check the three image rebases on their exact source commits."""
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile

MASTER = "2552c678794e048487e1d18e9c81729b67fc119a"
MYSQL = "25b9e2c269c513045f9c0f9232ae5b9846d2facb"
TOMCAT = "71dff5de7d5ed7441915d2425b7ba64c19de1c13"
HTTPD = "31a34e5765616b99908dd4bcb29e596bdf1a2858"


def output(*command: str) -> str:
    return subprocess.check_output(command, text=True).strip()


def checkout(revision: str) -> None:
    subprocess.run(["git", "switch", "--detach", revision], check=True)
    assert output("git", "rev-parse", "HEAD") == revision


def build(arch: str, targets: list[str]) -> None:
    subprocess.run(["bazel", "build", "--jobs=4", "--config=" + arch, *targets], check=True)


def read_json(archive: tarfile.TarFile, name: str) -> object:
    f = archive.extractfile(name)
    assert f is not None, name
    return json.load(f)


def check_contexts(revision: str, names: list[str]) -> None:
    checkout(revision)
    for cpu, arch in [("x86_64", "amd64"), ("aarch64", "arm64")]:
        db = output("make", "-np", "-f", "tools/images.mk", "ARCH=" + cpu,
                    "--eval=selection: ;", "selection").splitlines()
        labels = []
        for name in [*names, "basic/nginx", "basic/redis"]:
            package, fixture = name.split("/")
            label = f"//images/{package}:{fixture}_context"
            for operation in ["tag", "pull", "rebuild", "load", "test", "local-image", "push"]:
                mapping = f"{operation}-{package}_{fixture}: _image_context_target := {label}"
                assert mapping in db, (revision, cpu, mapping)
            if name in names:
                labels.append(label)
        build(cpu, labels)
        for name, label in zip(names, labels, strict=True):
            package, target = label.removeprefix("//").split(":")
            path = Path("bazel-bin", package, target + ".tar")
            with tarfile.open(path) as outer:
                files = [m for m in outer if m.isfile()]
                assert len(files) == 1, (path, [m.name for m in files])
                (image,) = files
                assert image.name.removeprefix("./") == f"images/{name}/image.tar", image.name
                payload = outer.extractfile(image)
                assert payload is not None, image
                with tarfile.open(fileobj=payload) as inner:
                    members = {m.name.removeprefix("./"): m.name for m in inner}
                    match read_json(inner, members["manifest.json"]):
                        case [{"Config": str(config), "RepoTags": tags}]:
                            assert f"gvisor.dev/images/{name}:latest" in tags, tags
                        case other:
                            raise ValueError(other)
                    match read_json(inner, members[config.removeprefix("./")]):
                        case {"architecture": actual, "os": "linux"}:
                            assert actual == arch, (name, actual, arch)
                        case other:
                            raise ValueError(list(other))
            print(f"{revision[:9]} {arch} {label} verified", flush=True)


def source_inventory(revision: str) -> dict[str, tuple[int, str]]:
    checkout(revision)
    build("x86_64", ["//images:source_contexts"])
    result = {}
    with tarfile.open("bazel-bin/images/source_contexts.tar") as archive:
        for member in archive:
            if member.isdir():
                continue
            path = member.name.removeprefix("./")
            assert path not in result, path
            if member.isfile():
                f = archive.extractfile(member)
                assert f is not None, path
                digest = hashlib.file_digest(f, "sha256").hexdigest()
            else:
                digest = member.linkname
            result[path] = member.mode, digest
    return result


def check_sources() -> None:
    before = source_inventory(MASTER)
    after = source_inventory(HTTPD)
    touched = set(output("git", "diff", "--name-only", MASTER, HTTPD).splitlines())
    added, removed = after.keys() - before.keys(), before.keys() - after.keys()
    assert not (added | removed) - touched, (added, removed, touched)
    for path in sorted(before.keys() & after.keys() - touched):
        assert before[path] == after[path], (path, before[path], after[path])
    others = sorted(path for path in before.keys() - touched if path.startswith("images/benchmarks/"))
    assert any(path.endswith("benchmarks/redis/Dockerfile") for path in others), others
    for line in output("git", "ls-tree", "-r", MASTER, "--", "images/benchmarks").splitlines():
        meta, path = line.split("\t", 1)
        if path in others:
            mode = int(meta.split()[0], 8) & 0o777
            assert after[path][0] == mode, (path, after[path][0], mode)
    print("Source archive:", len(before), "->", len(after), "files.",
          len(others), "unmodified benchmark paths verified:", *others, sep="\n", flush=True)
    print("Added:", sorted(added), "removed:", sorted(removed), flush=True)


if __name__ == "__main__":
    errors = []
    for revision, fixtures in [(MYSQL, ["basic/mysql"]), (TOMCAT, ["basic/tomcat"]),
                               (HTTPD, ["basic/httpd", "benchmarks/httpd"])]:
        try:
            check_contexts(revision, fixtures)
        except (AssertionError, KeyError, ValueError, OSError, subprocess.CalledProcessError) as e:
            errors.append((revision, str(e)))
            print(f"::error::{revision[:9]}: {e!r}", flush=True)
    try:
        check_sources()
    except (AssertionError, ValueError, OSError, subprocess.CalledProcessError) as e:
        errors.append(("sources", str(e)))
        print(f"::error::Apache sources: {e!r}", flush=True)
    if errors:
        raise SystemExit(errors)
