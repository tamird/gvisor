"""Join the release's original Go spawns to the archive set and Nogo facts."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess

EXAMPLES = {"reflect/internal/example1", "reflect/internal/example2", "net/internal/socktest",
            "log/slog/internal/benchmarks", "crypto/internal/sysrand/internal/seccomp"}
WANTED = {"GoStdlib", "GoStdlibList", "GoStandardLibraryAnalysis"}


def varint(f, eof_ok=False):
    value = 0
    for shift in range(0, 70, 7):
        b = f.read(1)
        if not b:
            if eof_ok and not shift:
                return None
            raise EOFError("truncated varint")
        value |= (b[0] & 127) << shift
        if b[0] < 128:
            return value
    raise ValueError("overlong varint")


def fields(b):
    from io import BytesIO
    s = BytesIO(b)
    while (tag := varint(s, True)) is not None:
        n, kind = divmod(tag, 8)
        if kind == 0:
            yield n, varint(s)
        else:
            size = varint(s) if kind == 2 else {1:8, 5:4}[kind]
            v = s.read(size)
            assert len(v) == size
            yield n, v


def signature(items):
    return {"count": len(items), "names_sha256": hashlib.sha256("\n".join(sorted(items)).encode()).hexdigest(),
            "excluded_test_packages": sorted(EXAMPLES & items)}


def package_archive_paths(files, arch):
    prefix = "linux_" + arch + "/"
    return {f[len(prefix):-2] for f in files if f.startswith(prefix) and f.endswith(".a") and
            not f.startswith(prefix + "cmd/")}


def source_packages(argv):
    inputs = [a for a in argv if "/src/" in a and a.endswith(".go") and not a.endswith("_test.go")
              and "/testdata/" not in a]
    stems = {str(Path(a.split("/src/", 1)[1]).parent) for a in inputs}
    return {s for s in stems if not s.startswith("cmd/") and s not in {".", "builtin", "cmd"}}


def read_spawns(log):
    objects = {}
    selected = []
    with subprocess.Popen(["zstd", "-qdc", log], stdout=subprocess.PIPE) as p:
        while (size := varint(p.stdout, True)) is not None:
            assert size < 256 << 20
            body = p.stdout.read(size)
            assert len(body) == size
            entry = dict(fields(body))
            if 3 in entry:
                file = dict(fields(entry[3]))
                path = file[1].decode()
                if path.endswith(("stdlib.facts", "stdlib.raw_findings", "stdlib.pkg.json")):
                    objects[entry[1]] = (path, None, dict(fields(file.get(2,b""))).get(1,b"").decode())
            if 4 in entry:
                items = list(fields(entry[4]))
                path = next(v.decode() for n,v in items if n == 1)
                if path.endswith("/pkg"):
                    objects[entry[1]] = (path, [dict(fields(v))[1].decode() for n,v in items if n == 2], "")
            if 7 not in entry:
                continue
            items = list(fields(entry[7]))
            spawn = dict(items)
            mnemonic = spawn.get(8,b"").decode()
            if mnemonic not in WANTED:
                continue
            outs = [objects[i] for n,v in items if n == 6 and (i := dict(fields(v)).get(5)) in objects]
            env = {d[1].decode(): d.get(2,b"").decode() for n,v in items if n == 2 and (d := dict(fields(v)))}
            selected.append((mnemonic, spawn.get(7,b"").decode().lstrip("@"), spawn.get(11,b"").decode(),
                             spawn.get(9,0), bool(spawn.get(12,0)), outs, env,
                             [v.decode() for n,v in items if n == 1]))
        assert p.wait() == 0
    return selected


def check(slot, arch, row, base):
    cpu = {"amd64":"k8", "arm64":"aarch64"}[arch]
    prefix = f"bazel-out/{cpu}-opt/bin/tools/nogo/stdlib."
    roots = list((base.STAGE / "home/.cache/bazel").glob("_bazel_*/*/execroot/_main"))
    assert len(roots) == 1
    logical = Path("/home/runner") / roots[0].relative_to(base.STAGE / "home")
    observed = read_spawns(slot / "spawns.zst")
    row["producer_counts"] = dict(Counter(m for m, *_ in observed))
    rows = []
    for m, owner, runner, code, cache, outs, env, args in observed:
        if m != "GoStandardLibraryAnalysis" or not any(p == prefix + "facts" for p,_,_ in outs):
            continue
        assert owner == "//tools/nogo:stdlib" and code == 0 and runner == "local" and not cache
        srcs = source_packages(args)
        record = {"owner": owner, "runner": runner, "GOROOT": env.get("GOROOT"), "sources": signature(srcs)}
        rows.append(record)
        facts = {}
        for path, _, sha in outs:
            f = base.container_file(logical/path)
            h = base.digest(f)
            assert h == sha or (not sha and f.stat().st_size == 0 and h == hashlib.sha256(b"").hexdigest())
            facts[path.rsplit(".",1)[1]] = {"sha256": h, "bytes": f.stat().st_size}
        assert set(facts) == {"facts","raw_findings"} and facts["facts"]["bytes"] > 0
        record["outputs"] = facts
        pkg_dir = env["GOROOT"].rstrip("/") + "/pkg"
        producers = [(o,r,archive) for mn,o,r,rc,cached,products,_,_ in observed
                     if mn == "GoStdlib" and rc == 0 and not cached for p,archive,_ in products if p == pkg_dir]
        assert len(producers) == 1 and producers[0][1] == "local", pkg_dir
        owner, runner, files = producers[0]
        packages = package_archive_paths(files, arch)
        record["producer"] = {"owner": owner, "runner": runner, "output": pkg_dir,
                              "archives": signature(packages), "without_source": sorted(packages-srcs)[:12]}
        assert len(srcs) > 100 and len(packages) > 100 and not packages-srcs and not packages & EXAMPLES
    row["checked_target_nogo"] = rows
    assert len(rows) == 1
    lists = []
    for m,o,runner,code,cache,outs,_,_ in observed:
        if m != "GoStdlibList":
            continue
        for p,_,sha in outs:
            f = base.container_file(logical/p)
            assert base.digest(f) == sha
            pkgs = {json.loads(line)["PkgPath"] for line in f.open() if line.strip()}
            lists.append({"owner":o,"runner":runner,"output":p,"sha256":sha,"packages":signature(pkgs)})
            assert code == 0 and not cache and runner == "local" and not pkgs & EXAMPLES
    row["executed_stdlib_lists"] = lists
