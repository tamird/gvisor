"""Collect the sole original Bazel test result for the fork-only comparison."""

import hashlib, json, os, pathlib, uuid, zipfile
from urllib.parse import unquote, urlparse

w = pathlib.Path(os.environ["RUNNER_TEMP"]) / "arm-kernel-pair"
dest = w / "receipt.json"
r = json.loads(dest.read_text())
r.update(stage="analyzed", process_exit=int((w / "exit").read_text()), verified=False)
label = "//.github/qualification/arm_tsync:kernel_pair"
try:
    rows, finish = [], []
    if (w / "run.bep").exists():
        for line in (w / "run.bep").open():
            e = json.loads(line)
            if "started" in e:
                r["invocation"] = str(uuid.UUID(e["started"]["uuid"]))
            if "testResult" in e:
                rows.append((e["id"]["testResult"], e["testResult"]))
            if "finished" in e:
                finish.append(e["finished"].get("exitCode", {}).get("code", 0))
    r["finished"] = finish
    r["attempts"] = len(rows)
    if len(rows) == 1:
        ident, t = rows[0]
        assert ident["label"].lstrip("@") == label
        assert all(ident.get(k, 1) == 1 for k in ("attempt", "run", "shard"))
        info = t.get("executionInfo", {})
        r.update(
            status=t["status"],
            strategy=info.get("strategy"),
            cached=bool(t.get("cachedLocally") or info.get("cachedRemotely")),
            originals={},
        )
        for item in t.get("testActionOutput", []):
            n = item["name"]
            if n not in (
                "test.log",
                "test.xml",
                "test.outputs__outputs.zip",
            ) and not n.endswith("/kernel-pair.json"):
                continue
            uri = urlparse(item["uri"])
            assert uri.scheme == "file" and not uri.netloc
            p = pathlib.Path(unquote(uri.path))
            assert p.is_file() and p.stat().st_size < 5 << 20
            raw = p.read_bytes()
            h = hashlib.sha256(raw).hexdigest()
            assert not item.get("digest") or item["digest"] == h
            r["originals"][n] = [h, len(raw)]
            if n.endswith("outputs.zip"):
                with zipfile.ZipFile(p) as z:
                    assert z.namelist() == ["kernel-pair.json"]
                    r["comparison"] = json.loads(z.read("kernel-pair.json"))
            elif n.endswith("/kernel-pair.json"):
                r["comparison"] = json.loads(raw)
        r["verified"] = (
            r["strategy"] in ("local", "standalone")
            and not r["cached"]
            and r["status"] == "PASSED"
            and r["process_exit"] == 0
            and finish == [0]
            and r.get("comparison", {}).get("comparison_verified")
            and "test.log" in r["originals"]
            and "test.xml" in r["originals"]
        )
finally:
    text = (w / "build.log").read_text(errors="replace")
    r["diagnostic_classes"] = [
        k
        for k, v in {
            "compile": "compilation of rule",
            "missing_target": "no such target",
            "visibility": "not visible",
            "toolchain": "no matching toolchain",
            "fetch": "error downloading",
            "disk": "no space left",
            "strategy": "cannot be executed with any",
            "timeout": "timed out",
            "missing_lib": "cannot find -l",
        }.items()
        if v in text.lower()
    ]
    dest.write_text(json.dumps(r, indent=2) + "\n")
raise SystemExit(not r["verified"])
