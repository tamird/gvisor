"""Inspect only the actions belonging to the two release stages and selected Flit producer."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback

WORK = Path(__file__).resolve().parent
SOURCE = os.environ["PRODUCT"]
ARCHIVES = "//debian:release-archives"
STAGE = "//debian:release-inputs"
DIST = "//sandboxexec/sandbox/python:dist"
REFERENCE = json.loads((WORK / "release_projection_reference.json").read_text())
QUERIES = {
    "stage": f'mnemonic("^(Symlink|ReleaseSHA512|StageReleaseDist|EmptyReleaseDist)$", set({ARCHIVES} {STAGE}))',
    "manifest": f'mnemonic("^FileWrite$", {STAGE})',
    "flit": f'mnemonic("^SandboxExecDist$", filter("{DIST}$", deps({STAGE})))',
}


def scrub(data):
    text = data.decode(errors="replace").replace(str(WORK), "<work>").replace(str(Path.home()), "<home>")
    lines = [line[:520] for line in text.splitlines()
             if not re.search(r"authorization|token|credential|api.key|https?://\S+\?", line, re.I)]
    return lines[:12] + (["…"] + lines[-12:] if len(lines) > 12 else [])


def observe(mode, part, row):
    ref = REFERENCE[mode]
    opts = ["-c", "opt", "--remote_executor=", "--remote_cache=", "--bes_backend=", "--lockfile_mode=off",
            "--jobs=3", "--noshow_progress", "--color=no", "--curses=no", "--remote_download_outputs=toplevel",
            "--//sandboxexec/sandbox/python:dist_version=" + ("2026.10.26.13" if mode == "tagged" else "")]
    if part == "manifest":
        opts.append("--include_file_write_contents")
    proc = subprocess.run([WORK / "bazel", "--nosystem_rc", "--nohome_rc", "aquery", *opts, "--output=jsonproto", "--include_artifacts", "--include_commandline", QUERIES[part]],
                          capture_output=True)
    row.update(exit=proc.returncode, stdout_sha256=hashlib.sha256(proc.stdout).hexdigest(),
               stderr_sha256=hashlib.sha256(proc.stderr).hexdigest(), stdout_bytes=len(proc.stdout),
               stderr=scrub(proc.stderr))
    if proc.returncode:
        raise RuntimeError("aquery returned " + str(proc.returncode))
    graph = {} if mode == "empty" and part == "flit" and not proc.stdout.strip() else json.loads(proc.stdout)
    assert len(graph.get("actions", [])) <= 20, "bounded owner query returned more actions than expected"
    targets = {str(v["id"]): v["label"].lstrip("@") for v in graph.get("targets", [])}
    fragments = {str(v["id"]): v for v in graph.get("pathFragments", [])}
    def path(i):
        fragment = fragments[str(i)]
        return (path(fragment["parentId"]) + "/" if fragment.get("parentId") else "") + fragment["label"]
    paths = {str(v["id"]):path(v["pathFragmentId"]) for v in graph.get("artifacts", [])}
    sets = {str(v["id"]):v for v in graph.get("depSetOfFiles", [])}
    def inputs(i):
        s = sets[str(i)]
        return {paths[str(j)] for j in s.get("directArtifactIds", [])}.union(
            *(inputs(j) for j in s.get("transitiveDepSetIds", [])))
    rows = row["actions"] = []
    root = ref["root"]
    for a in graph.get("actions", []):
        owner = targets[str(a["targetId"])]
        kind = a["mnemonic"]
        out = paths[str(a["primaryOutputId"])]
        observed = {"owner":owner, "type":kind, "out":out, "configuration":a["configurationId"]}
        rows.append(observed)
        if kind == "Symlink":
            relative = out.partition("/debian/" + owner.rsplit(":", 1)[1] + "/")[2]
            observed["in"] = sorted(set().union(*(inputs(x) for x in a["inputDepSetIds"])))
            assert owner in (ARCHIVES, STAGE) and observed["in"] == [ref["native"][relative]]
        elif kind == "ReleaseSHA512":
            observed["args"] = a["arguments"][-3:]
            relative = out.removeprefix(root).removesuffix(".sha512")
            assert owner == STAGE and observed["args"] == ["checksum",ref["native"][relative],out]
        elif kind in ("StageReleaseDist", "EmptyReleaseDist"):
            observed["args"] = a["arguments"][1:]
            expected = ["python",*ref["dist"],out] if mode == "tagged" else ["empty",out]
            assert owner == STAGE and out == root + "python" and observed["args"] == expected
        elif kind == "FileWrite":
            observed["manifest"] = a.get("fileContents", "")[:800]
            members = observed["manifest"].split("\0")
            assert owner == STAGE and out == root + "inputs"
            assert members[-1] == "" and len(members) == 7 and set(members[:-1]) == ref["native"].keys()
        elif kind == "SandboxExecDist":
            assert owner == DIST and [out] == ref["dist"]
        else:
            raise RuntimeError("unexpected configured action")
    if part == "stage":
        counts = {"Symlink":12, "ReleaseSHA512":4, ("StageReleaseDist" if mode == "tagged" else "EmptyReleaseDist"):1}
    elif part == "manifest":
        counts = {"FileWrite":1}
    else:
        counts = {"SandboxExecDist":1} if mode == "tagged" else {}
    assert Counter(v["type"] for v in rows) == counts
    if part == "stage":
        for owner in (STAGE,ARCHIVES):
            links = [v for v in rows if v["type"] == "Symlink" and v["owner"] == owner]
            assert {v["in"][0] for v in links} == set(ref["native"].values())
        assert {v["out"] for v in rows if v["type"] == "ReleaseSHA512"} == {
            root + k + ".sha512" for k in ref["native"] if not k.endswith(".deb")}


def run(mode):
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip() == SOURCE
    out = {"source":SOURCE,"mode":mode,"reference":REFERENCE["artifact"],"verified":False}
    for part in QUERIES:
        row = out[part] = {"query":QUERIES[part], "verified":False}
        try:
            observe(mode,part,row)
            row["verified"] = True
        except Exception as e:
            frame = traceback.extract_tb(e.__traceback__)[-1]
            row["error"] = {"kind":type(e).__name__, "line":frame.lineno, "detail":str(e)[:350]}
        (WORK/"public"/(mode+".json")).write_text(json.dumps(out,indent=2)+"\n")
    out["verified"] = all(out[p]["verified"] for p in QUERIES)
    (WORK/"public"/(mode+".json")).write_text(json.dumps(out,indent=2)+"\n")
    return out["verified"]


if __name__ == "__main__":
    sys.exit(not run(sys.argv[1]))
