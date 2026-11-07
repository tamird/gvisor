"""Inspect the configured action stream without executing project actions."""
from collections import Counter
from io import BytesIO
from pathlib import Path
import subprocess

import build_test as q


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


def packed(value):
    if isinstance(value, int):
        return [value]
    reader = BytesIO(value)
    out = []
    while (n := varint(reader, eof_ok=True)) is not None:
        out.append(n)
    return out


def closure(stream, names):
    fragments, artifacts, targets, dep_sets, producers, actions = {}, {}, {}, {}, {}, []
    while (size := varint(stream, eof_ok=True)) is not None:
        if size > 64000000:
            raise ValueError("oversized graph entry")
        message = stream.read(size)
        if len(message) != size:
            raise EOFError("truncated graph entry")
        for typ, raw in fields(message):
            if typ not in (1, 2, 3, 4, 8):
                continue
            if typ == 2:
                target = config = 0
                kind = program = platform = ""
                ins, outs = [], []
                for key, value in fields(raw):
                    if key == 1:
                        target = value
                    elif key == 4:
                        kind = value.decode()
                    elif key == 5:
                        config = value
                    elif key == 6 and not program:
                        program = value.decode()
                    elif key == 8:
                        ins.extend(packed(value))
                    elif key == 9:
                        outs.extend(packed(value))
                    elif key == 14:
                        platform = value.decode()
                for item in outs:
                    producers[item] = len(actions)
                actions.append((target, config, kind, program, platform, ins))
            elif typ == 4:
                transitive, direct, ident = [], [], 0
                for key, value in fields(raw):
                    if key == 1:
                        ident = value
                    elif key == 2:
                        transitive.extend(packed(value))
                    elif key == 3:
                        direct.extend(packed(value))
                dep_sets[ident] = transitive, direct
            else:
                data = dict(fields(raw))
                if typ == 1:
                    artifacts[data[1]] = data[2]
                elif typ == 3:
                    targets[data[1]] = data[2].decode().removeprefix("@@")
                else:
                    fragments[data[1]] = data[2].decode(), data.get(3, 0)
    suffixes = {Path(p).name for p in names}
    roots, pending = set(), []
    for art, frag in artifacts.items():
        if fragments[frag][0] not in suffixes:
            continue
        parts = []
        while frag:
            part, frag = fragments[frag]
            parts.append(part)
        name = "/".join(reversed(parts))
        if name in names:
            roots.add(name)
            pending.append(producers[art])
    runners = [i for i,(owner, _, kind, _, _, _) in enumerate(actions) if kind == "TestRunner" and targets[owner] == q.ROOT]
    pending.extend(runners)
    seen, seen_sets = set(), set()
    while pending:
        i = pending.pop()
        if i in seen:
            continue
        seen.add(i)
        sets = list(actions[i][5])
        while sets:
            item = sets.pop()
            if item in seen_sets:
                continue
            seen_sets.add(item)
            child, direct = dep_sets[item]
            sets.extend(child)
            pending.extend(producers[a] for a in direct if a in producers)
    kinds = Counter()
    known_absent = {"/bin/sh", "/bin/bash", "/usr/bin/env", "/bin/touch", "/usr/bin/touch"}
    unusual, sniffers = [], []
    for i in sorted(seen):
        owner, config, kind, program, platform, _ = actions[i]
        kinds[kind] += 1
        row = {"owner": targets[owner], "config_id": config, "kind": kind, "argv0": q.clean(program), "platform": q.clean(platform)}
        if program.startswith("/") or program.endswith(".sh") or kind == "Genrule":
            row["known_absent"] = program in known_absent
            unusual.append(row)
        if targets[owner] == "//pkg/test/dockerutil:run_sniffer_bin":
            sniffers.append(row)
    return {"configured_actions": len(actions), "reached_actions": len(seen), "root_files": len(roots),
            "root_test_actions": len(runners), "reached_mnemonics": dict(kinds), "shell_or_absolute": unusual[:100],
            "shell_or_absolute_count": len(unusual), "sniffer": sniffers, "complete": set(names) == roots and len(runners) == 1}


def scan(arch):
    name = "closure-" + arch
    flags = q.opts(arch, True, True)
    flags.remove("--spawn_strategy=remote")
    args = q.startup(True, False) + ["aquery", *flags, "--remote_cache=", "--remote_executor=", "--bes_backend=",
            "--output=streamed_proto", "--include_artifacts", "--include_commandline", "--noinclude_param_files", "deps(//:build_test)"]
    errfile = q.WORK / (name + ".stderr")
    with errfile.open("wb") as err, subprocess.Popen(args, stdout=subprocess.PIPE, stderr=err) as proc:
        try:
            result = closure(proc.stdout, q.report["cells"]["graph-" + arch]["labels"][q.ROOT])
        except (KeyError, IndexError, ValueError, EOFError) as exc:
            result = {"complete": False, "reader": type(exc).__name__, "detail": str(exc)[:200]}
            proc.stdout.close()
        result["exit"] = proc.wait()
    result["errors"] = q.local_detail(errfile.read_text(errors="replace"))
    q.report["cells"][name] = result
    q.save()
