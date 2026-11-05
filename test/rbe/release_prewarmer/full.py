"""Build only the two interrupted race guests on the shared executable output."""

from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback
import uuid
import zipfile

import qualify as q
import native_artifacts as native


PRODUCT = "e47624715b3e7be80ecc404bdc178c30ffb5a3f8"
PARENT = "9ccd97431e05ba31f7dd7754b68335513c8d422a"
PRODUCER = "90dd28479a2fbd8bbbf0c9bd79ad9e9f9abb5b4c"
SUPPLIER = "c2f5f79dce24877679cede14b85666b7ca65c6de"
ORIGINAL = "d0373b0553476e9b3b8f40ab70dc363d5b3d72cfd37a9b3aabe5a0e092741561"
BOOT_TARGET = "//runsc/boot:boot_test"
APP = "//test/cmd/test_app:test_app"
APP_BIN = "//test/cmd/test_app:test_app_bin"
BAZEL = q.TMP / "bin" / "bazel"


def preflight():
    """Download only the pinned public artifacts, with bounded transient retries."""
    artifacts = {
        "supplier.zip": (13078732581, 77817582, "993e832b8f926876f2b935e97af3cc50493a507e79ed80d1da32309f00e94619"),
        "original.zip": (13428223649, 14356, ORIGINAL),
        "images.zip": (13100242102, 4965, "5f919ba8d820862f00522ab10658beb6738f31c12550c1cda1d8e15ce5973859"),
    }
    row = {"status": "INCOMPLETE", "started": q.now(), "artifacts": {},
           "previous": {"run": 42059628045, "artifact": 13431745598,
                        "phase": "artifact download", "http_error_status": 503,
                        "bazel_commands": 0}}
    q.save("preflight", row)
    for name, (artifact, size, digest) in artifacts.items():
        item = row["artifacts"][name] = {"artifact": artifact, "expected_bytes": size,
                                        "expected_sha256": digest, "attempts": []}
        destination = q.TMP / name
        if destination.is_file() and destination.stat().st_size == size and hashlib.sha256(destination.read_bytes()).hexdigest() == digest:
            item["status"] = "CACHED_VERIFIED"
            q.save("preflight", row)
            continue
        partial = destination.with_suffix(".partial")
        for attempt, delay in enumerate((0, 5, 15), 1):
            if delay:
                time.sleep(delay)
            observation = {"attempt": attempt, "started": q.now()}
            item["attempts"].append(observation)
            q.save("preflight", row)
            transient = False
            try:
                with partial.open("wb") as out:
                    process = subprocess.run(["gh", "api", "repos/tamird/gvisor/actions/artifacts/" + str(artifact) + "/zip"],
                                             stdout=out, stderr=subprocess.PIPE, timeout=45)
                observation["exit"] = process.returncode
                statuses = re.findall(rb"HTTP ([0-9]{3})", process.stderr)
                status = int(statuses[-1]) if statuses else None
                observation["http_error_status"] = status
                transient = status in (408, 425, 429, 500, 502, 503, 504)
                if process.returncode == 0:
                    observation["bytes"] = partial.stat().st_size
                    if partial.stat().st_size == size:
                        observation["sha256"] = hashlib.sha256(partial.read_bytes()).hexdigest()
                    if observation.get("sha256") == digest:
                        partial.replace(destination)
                        item["status"] = "DOWNLOADED_VERIFIED"
                    else:
                        item["status"] = "INTEGRITY_FAILURE"
                        transient = False
                else:
                    item["status"] = "HTTP_FAILURE" if status else "DOWNLOAD_FAILURE"
            except subprocess.TimeoutExpired:
                item["status"] = "DOWNLOAD_TIMEOUT"
                transient = True
            except OSError as exc:
                item["status"] = "DOWNLOAD_FAILURE"
                observation["error_type"] = type(exc).__name__
            finally:
                partial.unlink(missing_ok=True)
                observation["finished"] = q.now()
                q.save("preflight", row)
            if item["status"] == "DOWNLOADED_VERIFIED" or not transient:
                break
    row["status"] = "PASS" if all(item["status"] in ("DOWNLOADED_VERIFIED", "CACHED_VERIFIED")
                                  for item in row["artifacts"].values()) else "INCOMPLETE"
    row["verified"] = q.now()
    q.save("preflight", row)
    return row["status"] == "PASS"


def original(arch):
    raw = (q.TMP / "original.zip").read_bytes()
    assert len(raw) == 14356 and hashlib.sha256(raw).hexdigest() == ORIGINAL
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        prior = json.loads(archive.read(arch + "-boot-fixture.json"))
        runtime = json.loads(archive.read(arch + "-boot-full.json"))
    assert prior["source"] == runtime["source"] == PARENT
    normal, race = prior["modes"]["normal"], prior["modes"]["race"]
    assert normal["status"] == "PASS" and normal["native_exit"] == 0
    assert race["status"] == "INCOMPLETE" and race["native_exit"] == 124 and race["events"]["exit"] == 8
    assert race["owner"]["aborted"] == ["USER_INTERRUPTED"]
    assert not race["actions"]["links"] and not race["owner"]["results"]
    result, = runtime["targets"][BOOT_TARGET]["results"]
    assert result["xml"]["counts"] == {"passed": 87, "failed": 0, "skipped": 0}
    return {"artifact": 13428223649, "zip_sha256": ORIGINAL, "source": PARENT,
            "race_invocation": race["events"]["invocation"], "native_exit": 124, "bazel_exit": 8,
            "actions": race["actions"]["runners"], "normal_elf": normal["elf"],
            "prior_runtime_counts": result["xml"]["counts"], "prior_runtime_xml": result["xml"]["sha256"]}


def source():
    assert q.git("rev-parse", "HEAD") == PRODUCT and not q.git("diff", "--name-only", "HEAD")
    assert q.git("rev-parse", "HEAD^") == PARENT
    assert q.git("diff", "--name-only", "HEAD^", "HEAD") == "test/cmd/test_app/BUILD"
    assert q.git("rev-parse", "HEAD:test/cmd/test_app/BUILD") == "19d875110d4ac6cce37a1e0668b251a7ffcc31e8"
    assert q.git("rev-parse", "HEAD:runsc") == "0692e10b99760793ec68bd1a160006f83ef5f194"
    assert q.git("rev-parse", "HEAD:tools/rules_go_bootstrap_shell.patch") == "033fde5646ef43acaecae9b033ec8f867200e8c1"
    assert '"GVISOR_BOOT_TEST_APP": "$(rootpath //test/cmd/test_app)"' in q.git("show", "HEAD:runsc/boot/BUILD")


def common_flags(arch):
    platform = "//test/rbe/release_prewarmer:" + arch
    return ["--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch],
            "--extra_execution_platforms=" + platform, "--host_platform=" + platform,
            "--extra_toolchains=//test/rbe/go_bootstrap:linux_" + arch + "_ash",
            "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
            "--extra_toolchains=//test/rbe/protoc_check:static_python" + ("_arm64" if arch == "arm64" else ""),
            "--noincompatible_exclude_starlark_flags_from_exec_config", "--lockfile_mode=off",
            "--jobs=32", "--color=no", "--curses=no"]


def identity(label):
    return label.removeprefix("@@").removeprefix("@")


def fault(exc):
    return {"type": type(exc).__name__, "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}


def events(path, targets, configs):
    data = {"failures": [], "aborted": [], "malformed_lines": 0, "test_results": 0}
    if not path.is_file():
        return data
    for line in path.open():
        try:
            event = json.loads(line)
        except ValueError:
            data["malformed_lines"] += 1
            continue
        ident = event.get("id", {})
        if "started" in event:
            data["invocation"] = event["started"]["uuid"]
        if "finished" in event:
            data.update(exit=event["finished"]["exitCode"].get("code", 0), finished=event["finished"].get("finishTime"))
        if "configuration" in event:
            configs[ident["configuration"]["id"]] = event["configuration"].get("cpu")
        if (key := ident.get("targetCompleted")) and identity(key["label"]) in targets and not key.get("aspect"):
            targets[identity(key["label"])]["completed"].append({"config": key["configuration"]["id"],
                "built": bool(event.get("completed", {}).get("success"))})
        data["test_results"] += int("testResult" in ident)
        if (action := event.get("action")) and not action.get("success") and len(data["failures"]) < 16:
            data["failures"].append({"owner": ident.get("actionCompleted", {}).get("label"), "type": action.get("type"),
                "codes": [v["code"] for v in action.get("failureDetail", {}).values() if isinstance(v, dict) and "code" in v]})
        if "aborted" in event:
            aborted = {"owner": next((v.get("label") for v in ident.values() if isinstance(v, dict)), None),
                       "reason": event["aborted"].get("reason")}
            if len(data["aborted"]) < 16:
                data["aborted"].append(aborted)
            if aborted["owner"] and identity(aborted["owner"]) in targets:
                targets[identity(aborted["owner"])]["aborted"].append(aborted["reason"])
    return data


FIXTURE_EXPR = """json.encode({
  "label": str(target.label),
  "cpu": build_options(target)["//command_line_option:cpu"],
  "race": {k: v for k, v in build_options(target).items() if k.endswith("//go/config:race")},
  "files": [f.path for f in target.files.to_list()],
  "executable": providers(target)["DefaultInfo"].files_to_run.executable.path,
  "runfiles": [{"path": f.path, "short_path": f.short_path} for f in providers(target)["DefaultInfo"].default_runfiles.files.to_list() if "/bin/test/cmd/test_app/" in f.path]
})"""
FIXTURE_SELECT = r"""
if .spawn then .spawn |
{runner,mnemonic,cache:(.cacheHit//false),exit:(.exitCode//0),status:((.status//"")!=""),
 platform:[.platform.properties[]? | select(.name|IN("Arch","OSFamily","container-image","recycle-runner","workload-isolation-type"))]} +
(if .mnemonic=="GoLink" and (.targetLabel|test("^(@@?)?//test/cmd/test_app:")) then
 {label:.targetLabel,digest,outputs,race:(.args|index("-race")!=null)} elif ((.exitCode//0)!=0 or (.status//"")!="") then {failed_label:.targetLabel,digest} else {} end)
elif .file and (.file.path|contains("/bin/test/cmd/test_app/")) then
 {file:{id,path:.file.path,digest:.file.digest}}
else empty end
"""


def fixture_actions(path, arch):
    expected = {"Arch": arch, "OSFamily": "linux", "container-image": "docker://gcr.io/distroless/static-debian13@" + q.PIN[arch],
                "recycle-runner": "false", "workload-isolation-type": "oci"}
    counts, files, links, failures = Counter(), {}, [], []
    with subprocess.Popen([str(q.TMP / "bb"), "print", "--raw", "--compact_execution_log=" + str(path)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as decoder:
        with subprocess.Popen(["jq", "-c", FIXTURE_SELECT], stdin=decoder.stdout, stdout=subprocess.PIPE, text=True) as rows:
            decoder.stdout.close()
            for line in rows.stdout:
                item = json.loads(line)
                if "file" in item:
                    f = item["file"]
                    files[f["id"]] = {"path": f["path"], "digest": f["digest"]}
                    continue
                props = {p["name"]: p["value"] for p in item.pop("platform")}
                counts[item["runner"]] += 1
                if item["runner"] not in ("remote", "remote cache hit") or (not item["cache"] and props != expected):
                    counts["wrong_execution"] += 1
                if item["exit"] or item["status"]:
                    counts["failed_actions"] += 1
                    if len(failures) < 16:
                        failures.append(dict(item))
                if item["mnemonic"] == "TestRunner":
                    counts["TestRunner"] += 1
                if "label" in item:
                    item["worker_matches"] = props == expected
                    links.append(item)
            assert rows.wait() == 0
        assert decoder.wait() == 0
    for link in links:
        link["outputs"] = [files[o["outputId"]] for o in link.pop("outputs", []) if o.get("outputId") in files]
    return {"runners": dict(counts), "links": links, "failures": failures}


def fixture(arch):
    name = arch + "-boot-race-fixture"
    row = {"source": PRODUCT, "parent": PARENT, "producer": PRODUCER, "arch": arch,
           "scope": "race guest build and configured boot data edge; zero tests or guest execution",
           "status": "INCOMPLETE", "started": q.now()}
    q.save(name, row)
    work = q.TMP / name
    work.mkdir()
    try:
        source()
        row["original"] = original(arch)
        row["worker_image"] = native.image(arch)
        cpu = {"amd64": "k8", "arm64": "aarch64"}[arch]
        flags = common_flags(arch) + ["--config=race", "--keep_going", "--remote_accept_cached",
                 "--remote_download_outputs=toplevel", "--nobuild_event_json_file_path_conversion", "--build_event_publish_all_actions"]
        # The configured dependency path is retained even if the later build times out.
        query_id = str(uuid.uuid4())
        query = "allpaths(" + BOOT_TARGET + ", " + APP_BIN + ")"
        row["requested_cquery_invocation"] = query_id
        with (work / "providers").open("wb") as out, (work / "query-log").open("wb") as log:
            child = subprocess.run(["timeout", "--signal=INT", "--kill-after=10", "120", str(BAZEL), *q.PROOF,
                "cquery", *flags, "--invocation_id=" + query_id, "--build_event_json_file=" + str(work / "query-events"),
                "--output=starlark", "--starlark:expr=" + FIXTURE_EXPR, query], stdout=out, stderr=log)
        row["cquery_exit"] = child.returncode
        row["cquery_diagnostics"] = q.diagnostics(work / "query-log")
        row["cquery_events"] = events(work / "query-events", {}, {})
        q.save(name, row)
        qe = row["cquery_events"]
        assert child.returncode == qe.get("exit") == 0 and qe.get("invocation") == query_id
        assert not (qe["test_results"] or qe["failures"] or qe["malformed_lines"])
        assert all(x["reason"] == "NO_BUILD" for x in qe["aborted"])
        assert (work / "providers").stat().st_size <= 64 * 1024
        providers = [json.loads(line) for line in (work / "providers").read_text().splitlines()]
        row["configured_path"] = providers
        q.save(name, row)
        assert len(providers) == 3
        providers = {identity(p["label"]): p for p in providers}
        assert set(providers) == {APP, APP_BIN, BOOT_TARGET}
        assert all(p["cpu"] == cpu and list(p["race"].values()) == [True] for p in providers.values())
        app, = providers[APP]["files"]
        inner, = providers[APP_BIN]["files"]
        assert providers[APP]["executable"] == app
        for path in (app, inner, providers[APP_BIN]["executable"]):
            assert re.fullmatch(r"bazel-out/[A-Za-z0-9_.+-]+/bin/test/cmd/test_app/[A-Za-z0-9_./+-]+", path)
            assert ".." not in Path(path).parts
        runfile, = [f for f in providers[BOOT_TARGET]["runfiles"] if f["path"] == app]
        assert runfile["short_path"] == "test/cmd/test_app/test_app" == app.split("/bin/", 1)[1]
        invocation = str(uuid.uuid4())
        row.update(requested_invocation=invocation, build_started=q.now(), build_limit_seconds=1500)
        q.save(name, row)
        with (work / "log").open("wb") as log:
            child = subprocess.run(["timeout", "--signal=INT", "--kill-after=30", "1500",
                str(BAZEL), *q.PROOF, "build", *flags, "--invocation_id=" + invocation,
                "--build_event_json_file=" + str(work / "events"), "--execution_log_compact_file=" + str(work / "spawns"), APP],
                stdout=log, stderr=subprocess.STDOUT)
        row.update(native_exit=child.returncode, returned=q.now(), diagnostics=q.diagnostics(work / "log"))
        if child.returncode not in (0, 124, 137):
            row["status"] = "FAIL"
        q.save(name, row)
        targets = {APP: {"completed": [], "aborted": []}}
        configs = {}
        row["events"] = events(work / "events", targets, configs)
        row["owner"] = targets[APP]
        q.save(name, row)
        if (work / "spawns").is_file():
            row["actions"] = fixture_actions(work / "spawns", arch)
        q.save(name, row)
        assert child.returncode == row["events"].get("exit") == 0
        assert row["events"].get("invocation") == invocation and not row["events"]["malformed_lines"]
        assert not (row["events"]["test_results"] or row["events"]["failures"] or row["events"]["aborted"])
        completed, = targets[APP]["completed"]
        assert completed["built"] and configs[completed["config"]] == cpu
        assert not any(row["actions"]["runners"].get(k) for k in ("wrong_execution", "failed_actions", "TestRunner"))
        root = Path("bazel-out").resolve(strict=True)
        path = Path(app)
        inner_path = Path(inner).resolve(strict=True)
        assert path.is_symlink() and inner_path.is_relative_to(root)
        assert path.resolve(strict=True) == inner_path
        assert Path(providers[APP_BIN]["executable"]).resolve(strict=True) == inner_path
        row["delivery"] = {"type": "native_binary_symlink", "public_file": app,
                           "source_executable": providers[APP_BIN]["executable"], "underlying_file": inner,
                           "resolved_same_file": True}
        binary = row["elf"] = native.elf(inner_path, arch)
        assert binary["entry"] and not binary["interpreter"] and not binary["needed"]
        row["matching_link_outputs"] = []
        q.save(name, row)
        for link in row["actions"]["links"]:
            for output in link["outputs"]:
                if output["path"] == inner:
                    assert output["digest"]["hash"] == binary["sha256"] and int(output["digest"]["sizeBytes"]) == binary["bytes"]
                    assert not link["exit"] and not link["status"] and link["race"]
                    assert link["cache"] or link["worker_matches"]
                    row["matching_link_outputs"].append({"label": identity(link["label"]), "cache": link["cache"],
                        "race_arg": link["race"], "worker_matches": link["worker_matches"], "digest": output["digest"]})
        assert len(row["matching_link_outputs"]) == 1
        row["status"] = "PASS"
    except (OSError, AssertionError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        row["collection_error"] = fault(exc)
    finally:
        row["verified"] = q.now()
        q.save(name, row)
    return row["status"] == "PASS"


if __name__ == "__main__":
    if sys.argv[1:] == ["preflight"]:
        good = preflight()
    elif sys.argv[1:] == ["verdict"]:
        expected = [arch + "-boot-race-fixture" for arch in ("amd64", "arm64")]
        results = {name: json.loads((q.OUT / (name + ".json")).read_text()) for name in expected
                   if (q.OUT / (name + ".json")).exists()}
        statuses = {name: result["status"] for name, result in results.items()}
        preflight_path = q.OUT / "preflight.json"
        before = json.loads(preflight_path.read_text()) if preflight_path.is_file() else {"status": "NOT_REACHED"}
        good = statuses == dict.fromkeys(expected, "PASS")
        q.save("verdict", {"source": PRODUCT, "supplier": SUPPLIER, "carrier": os.environ["GITHUB_SHA"],
            "original_artifact": 13428223649, "preflight": before, "statuses": statuses, "pass": good,
            "scope": "build-only; no new TestRunner or guest execution", "verified_at": q.now()})
    else:
        assert sys.argv[1:] == ["paired"]
        results = [fixture(arch) for arch in ("amd64", "arm64")]
        good = all(results)
    raise SystemExit(0 if good else 1)
