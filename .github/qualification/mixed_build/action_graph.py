"""Fingerprint declared product recipes while retaining target/exec distinctions."""
from collections import defaultdict
from functools import cache
import hashlib
import json
import re


ROOT = re.compile(r"bazel-out/[^/\s\x00]+/(?=(?:bin|genfiles|testlogs)/)")
NATIVE = {"GoCompilePkg", "GoLink", "GoAsm", "CppCompile", "CppLink",
          "CppArchive", "LtoBackend", "GoCcLink"}


def norm(value):
    return ROOT.sub("bazel-out/<config>/", value)


def sha(value):
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        v = data[offset]
        offset += 1
        value |= (v & 127) << shift
        if v < 128:
            return value, offset
    raise ValueError("invalid varint")


def fields(data):
    out = defaultdict(list)
    offset = 0
    while offset < len(data):
        tag, offset = varint(data, offset)
        wire = tag & 7
        if wire == 0:
            value, offset = varint(data, offset)
        else:
            if wire == 2:
                n, offset = varint(data, offset)
            else:
                n = {1: 8, 5: 4}[wire]
            value = data[offset:offset+n]
            assert len(value) == n
            offset += n
        out[tag >> 3].append(value)
    return out


def ids(values):
    out = []
    for item in values:
        if isinstance(item, int):
            out.append(item)
        else:
            pos = 0
            while pos < len(item):
                value, pos = varint(item, pos)
                out.append(value)
    return tuple(out)


def messages(path):
    with path.open("rb") as f:
        while True:
            prefix = f.read(1)
            if not prefix:
                return
            while prefix[-1] >= 128:
                assert len(prefix) < 10
                next_byte = f.read(1)
                if not next_byte:
                    raise ValueError("truncated record length")
                prefix += next_byte
            n, _ = varint(prefix, 0)
            data = f.read(n)
            assert len(data) == n
            for tag, group in fields(data).items():
                for raw in group:
                    yield tag, fields(raw)


def kv(data):
    entry = fields(data)
    return tuple(norm(v.decode(errors="replace")) for n in (1, 2) for v in entry[n])


def graph(path):
    artifacts, fragments, targets, configs, depsets = {}, {}, {}, {}, {}
    actions, producers = [], {}
    for which, item in messages(path):
        idx = next(iter(item[1]), 0)
        if which == 1:
            artifacts[idx] = item[2][0]
        elif which == 3:
            targets[idx] = item[2][0].decode().lstrip("@")
        elif which == 4:
            depsets[idx] = ids(item[2]), ids(item[3])
        elif which == 5:
            configs[idx] = bool(item[5] and item[5][0]), item[4][0].decode()
        elif which == 8:
            fragments[idx] = item[2][0].decode(), next(iter(item[3]), 0)
        elif which == 2:
            mnemonic = item[4][0].decode()
            argv = [norm(v.decode(errors="replace")) for v in item[6]]
            params = []
            for raw in item[12]:
                p = fields(raw)
                params.append(tuple(norm(v.decode(errors="replace")) for n in (1, 2) for v in p[n]))
            env = sorted(kv(v) for v in item[7])
            cpu = dict(env).get("GOARCH", "")
            for ix, arg in enumerate(argv):
                if arg in ("-target", "--target", "-goarch", "--goarch") and ix + 1 < len(argv):
                    cpu = argv[ix+1]
                elif arg.startswith(("--target=", "-target=")):
                    cpu = arg.split("=", 1)[1]
            cpumask = (1 if cpu.split("-")[0] in ("amd64", "x86_64") else
                       2 if cpu.split("-")[0] in ("arm64", "aarch64") else 0)
            contents = [norm(v.decode(errors="replace")) for n in (15, 17) for v in item[n]]
            substitutions = [kv(v) for v in item[16]]
            # An action without an exposed command, file content or template
            # can have additional key material outside this analysis.
            opaque = not (argv or contents or substitutions or params)
            key = sha([mnemonic, argv, env, params, contents, substitutions, [norm(v.decode(errors="replace")) for v in item[18]], item[19]])
            outputs = ids(item[9])
            ai = len(actions)
            actions.append((idx, next(iter(item[5]), 0), mnemonic, key, ids(item[8]), opaque, cpumask))
            for oid in outputs:
                producers[oid] = ai
    @cache
    def frag(i):
        name, parent = fragments[i]
        return frag(parent) + "/" + name if parent else name
    paths = {i: frag(f) for i, f in artifacts.items()}
    by_path = {s: i for i, s in paths.items()}
    # Derive a structural fingerprint recursively for target actions. A tool
    # built for the executor has its logical identity, so equal recipes do not
    # assert identical tool bytes across native executors.
    @cache
    def dset(i):
        children, files = depsets[i]
        return combine("deps", [dset(c) for c in children] + [artifact(f) for f in files])
    @cache
    def act(i):
        target, cfg, mnemonic, key, inputs, opaque, cpumask = actions[i]
        if configs.get(cfg, (False, None))[0]:
            return sha(["tool", targets.get(target, "unknown")]), False, False, 0
        if mnemonic in NATIVE:
            # Preserve the actual target's compiler arguments and environment,
            # without recursively rehashing entire compiler sysroots.
            return sha(["compiled", key]), True, True, cpumask
        digest, native, missing, mask = combine(key, [dset(s) for s in inputs])
        return digest, native, missing or opaque, mask
    def combine(seed, values):
        return (sha([seed, sorted(v[0] for v in values)]),
                any(v[1] for v in values), any(v[2] for v in values),
                (int(any(v[3] & 1 for v in values)) | (2 * int(any(v[3] & 2 for v in values)))))
    @cache
    def artifact(i):
        p = paths[i]
        if not p.startswith("bazel-out/"):
            return sha(["source", p]), False, False, 0
        if i not in producers:
            return sha(["unexpanded", norm(p)]), False, "-exec-" not in p.split("/")[1], 0
        d, n, m, c = act(producers[i])
        return sha([norm(p), d]), n, m, c

    def inspect(files):
        result = []
        for is_source, p in files:
            if is_source:
                result.append((norm(p), sha(["source", p]), False, False, 0, "source"))
                continue
            i = by_path.get(p)
            if i is None or i not in producers:
                result.append((norm(p), sha(["unexpanded", norm(p)]), False, True, 0, "unexpanded"))
                continue
            d, n, m, c = artifact(i)
            result.append((norm(p), d, n, m, c, actions[producers[i]][2]))
        result.sort()
        return {"product_names": sha([r[0] for r in result]),
                "recipe": sha([(r[0], r[1]) for r in result]),
                "native": any(r[2] for r in result), "opaque": any(r[3] for r in result),
                "outputs": len(result), "native_cpu_flags":sorted({name for r in result for mask,name in ((1,"amd64"),(2,"arm64")) if r[4] & mask}),
                "source_only": all(r[5] == "source" for r in result),
                "mnemonics": sorted({r[5] for r in result})}
    return inspect
