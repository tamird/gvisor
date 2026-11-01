"""Observe the original GNU plugin Make rule on the native tool-free worker."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "plugin-evidence"
TARGET = "@libpluginstack//:libpluginstack"
OWNER = "@@+_repo_rules+libpluginstack//:libpluginstack"
NATIVE = "docker://gcr.io/distroless/static-debian13@sha256:d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18"
BASE = "7dc8df3c4bb7da1bc04037fe9a2d641cb36cc5a4"
OLD = "6fea7f1fe69df20db5f2358d5ecdeefba46ea16303228d402e900c626a08521a/351"


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save(r):
    (OUT / "result.json").write_text(json.dumps(r, indent=2) + "\n")


def diagnostics(text):
    # Extract just tool names and fixed status phrases, never environment or
    # raw command lines from foreign_cc's failure dump.
    found = []
    patterns = [
        r"([\w./+-]{1,150}): (command not found|applet not found|No such file or directory|not found|Permission denied|cannot execute binary file)",
        r"(cannot execute): (required file not found)",
        r"([\w./+-]{1,120}): (unrecognized option|invalid option|unknown option)",
        r"([\w./+-]{1,120}): (invalid applet|not supported)",
        r"(fatal error): ([\w./+-]{1,120}: No such file or directory)",
        r"(error): (unknown type name [\w']{1,60})",
        r"(No rule to make target) [‘'\x60]([\w./+-]{1,150})",
        r"(Makefile|[\w./+-]{1,120}\.mk):(\d+: [\w./+-]{1,100})",
        r"(BUILD|MODULE\.bazel|framework\.bzl):(\d+:\d+): ",
        r"(error applying patch|Checksum was|no such package|no such target|Unable to find package|does not exist|invalid label|syntax error|Failed to load Starlark extension|Error in repository |inconsistent exit status)()",
    ]
    for line in text.splitlines():
        if re.search(r"authorization|api[-_]?key|remote_header|bes_header|token|credential|https?://", line, re.I):
            continue
        for regex in patterns:
            for match in re.finditer(regex, line, re.I):
                parts = [part for part in match.groups() if part]
                message = " ".join(parts)
                if message not in found and len(found) < 60:
                    found.append(message)
    stages = ["Building libpluginstack", "generating custom config", "Build complete",
              "Foreign Cc Rules for Make", "ERROR: BUILD FAILED", "building dpdk"]
    return {"observed_stages": [s for s in stages if s.lower() in text.lower()],
            "diagnostics": found}


def audit(compact):
    query = """select(.spawn != null) | .spawn | select(.targetLabel == $owner and .mnemonic == "CcMakeMakeRule")
        | {mnemonic, targetLabel, args:(.args[0:2]), digest, runner, cacheHit, exitCode, status,
        platform:[.platform.properties[]?|select(.name=="Arch" or .name=="OSFamily" or
               .name=="container-image" or .name=="workload-isolation-type" or .name=="recycle-runner")]}"""
    with subprocess.Popen([str(TMP / "bb"), "print", "--raw", "--compact_execution_log=" + str(compact)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as reader:
        rows = subprocess.run(["jq", "-c", "--arg", "owner", OWNER, query], stdin=reader.stdout,
                              stdout=subprocess.PIPE, text=True, check=True)
        reader.stdout.close()
        assert reader.wait() == 0
    return [json.loads(line) for line in rows.stdout.splitlines()]


def main():
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    r = {"source": head, "base": BASE, "target": TARGET, "old_invocation": "9125942b-1fc2-42df-b235-6dee4e0ae37a",
         "old_action": OLD, "old_launch": "/bin/bash", "old_exit": 1, "worker": NATIVE,
         "started": now(), "status": "INCOMPLETE"}
    save(r)
    try:
        assert head == os.environ["GITHUB_SHA"]
        assert subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip() == BASE
        assert subprocess.run(["git", "diff", "--quiet", "HEAD", "--"]).returncode == 0
        assert json.loads((OUT / "image.json").read_text())["manifest"] == NATIVE.split("@")[1]
        bazel = ["bazel", "--bazelrc=" + str(TMP / "sidecar.bazelrc")]
        plat = "//test/rbe/empty_worker:amd64_musl"
        bep = TMP / "plugin.bep"
        compact = TMP / "plugin.compact"
        flags = ["--config=x86_64", "--config=plugin-tldk", "-c", "opt", "--host_platform=" + plat,
                 "--extra_execution_platforms=" + plat, "--remote_accept_cached",
                 "--noremote_upload_local_results", "--jobs=400", "--remote_download_outputs=minimal",
                 "--remote_download_regex=(^|/)libpluginstack[.]a$", "--noremote_local_fallback",
                 "--spawn_strategy=remote", "--color=no", "--curses=no",
                 "--registry=" + (Path(os.environ["GITHUB_WORKSPACE"]) / "registry").as_uri(),
                 "--registry=https://bcr.bazel.build", "--build_event_json_file=" + str(bep),
                 "--build_event_json_file_path_conversion=false", "--execution_log_compact_file=" + str(compact)]
        log = TMP / "plugin.log"
        with log.open("w") as dst:
            rc = subprocess.run(["timeout", "--signal=INT", "--kill-after=25s", "38m",
                                 *bazel, "build", *flags, TARGET], stdout=dst, stderr=subprocess.STDOUT).returncode
        r.update(build_exit=rc, build_finished=now(), status="FAIL" if rc else "INCOMPLETE")
        save(r)
        r.update(diagnostics(log.read_text(errors="replace")))
        save(r)
        files = []
        for line in bep.open():
            event = json.loads(line)
            if "started" in event:
                r["invocation"] = event["started"]["uuid"]
            if "buildFinished" in event["id"]:
                finished = event["finished"]
                r["bazel_finished"] = {k: finished.get(k) for k in ("exitCode", "finishTime")}
            if "targetCompleted" in event["id"] and event["id"]["targetCompleted"]["label"] in (TARGET, OWNER) and "aspect" not in event["id"]["targetCompleted"]:
                r["target_completed"] = bool(event.get("completed", {}).get("success"))
            for file in event.get("namedSetOfFiles", {}).get("files", []):
                if file["name"].endswith("/libpluginstack.a"):
                    files.append(file)
        save(r)
        actions = audit(compact)
        r["make_actions"] = actions
        save(r)
        assert len(actions) == 1
        action, = actions
        props = {item["name"]: item["value"] for item in action["platform"]}
        assert props["container-image"] == NATIVE and props["Arch"] == "amd64"
        assert props["recycle-runner"] == "false" and props["workload-isolation-type"] == "oci"
        assert action["runner"] == "remote" and not action["cacheHit"] and action["args"][0].endswith("plugin_programs/bin/bash")
        assert action["args"][1].endswith("wrapper_build_script.sh")
        r["verified_launch"] = True
        if rc == 0:
            assert not action["exitCode"] and r["target_completed"] and files
            from urllib.parse import unquote, urlparse
            archives = {Path(unquote(urlparse(f["uri"]).path)) for f in files}
            assert len(archives) == 1
            path, = archives
            data = path.read_bytes()
            assert data.startswith(b"!<arch>\n")
            symbols = subprocess.check_output(["nm", "--defined-only", "--extern-only", path], text=True,
                                              stderr=subprocess.DEVNULL)
            required = ["plugin_socket", "plugin_epoll_wait", "plugin_get_port_range"]
            assert all(re.search(r"\bT " + name + r"$", symbols, re.M) for name in required)
            r["archive"] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "defined": required}
            r["status"] = "PASS"
    except Exception as exc:
        import traceback
        r["collection_error"] = {"type": type(exc).__name__, "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}
    r["source_clean"] = subprocess.run(["git", "diff", "--quiet", "HEAD", "--"]).returncode == 0
    if not r["source_clean"] and r["status"] == "PASS":
        r["status"] = "INCOMPLETE"
    r["verdict_time"] = now()
    save(r)
    return 0 if r["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
