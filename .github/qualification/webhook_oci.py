"""Check the same packaged entrypoint with and without its four init-container files."""

import datetime
import hashlib
import io
import json
import os
import platform
import re
import subprocess
import tarfile
import zipfile
from pathlib import Path


WORK = Path(os.environ["RUNNER_TEMP"]) / "webhook-oci"
PRODUCT = "0bdc5723e11795c6063c95d36fba0c70817eacbc"
CASES = ["retained receipts", "AMD64 image and ELF identity", "empty certificate directory", "mounted fixture help"]
RESULT = {"expected": CASES, "executed": [], "passed": [], "failed": [], "observations": {}}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def probe(case, callback):
    RESULT["executed"].append(case)
    print(case, flush=True)
    try:
        callback()
        RESULT["passed"].append(case)
        return True
    except Exception as error:
        reason = str(error) if isinstance(error, CheckFailure) else type(error).__name__
        failure = {"case": case, "error_type": type(error).__name__, "reason": reason}
        if "command_error" in RESULT:
            failure["command_error"] = RESULT.pop("command_error")
        RESULT["failed"].append(failure)
        return False


class CheckFailure(Exception):
    """A fixed diagnostic safe for publication."""


def check(condition, reason):
    if not condition:
        raise CheckFailure(reason)


def diagnostic(args, proc):
    text = (proc.stderr + proc.stdout).decode(errors="replace")
    # Only literal signatures and local source locations enter the artifact.
    signatures = (
        "no such attribute", "no such target", "not executable", "permission denied",
        "not visible", "has no default", "is not allowed", "not a valid",
        "cannot load", "no matching", "manifest unknown", "unauthorized",
        "connection refused", "no such file or directory", "not found",
        "400 Bad Request", "401 Unauthorized", "404 Not Found", "500 Internal",
        "unknown flag", "unknown command", "no tags", "invalid reference",
        "no space left", "failed to fetch", "repository rule", "deadline exceeded",
        "server gave HTTP response to HTTPS client", "no such manifest",
        "certificate signed by unknown authority", "invalid arithmetic operator",
        "cannot index", "push first", "syntax error", "unknown blob", "manifest invalid",
    )
    verbs = {"manifest", "inspect", "create", "push", "image", "tag", "run", "build", "cquery", "version", "info"}
    return {
        "tool": args[0], "verb": [a for a in args[1:4] if a in verbs], "exit": proc.returncode,
        "signatures": [s for s in signatures if s.lower() in text.lower()],
        "locations": sorted(set(re.findall(r"(?:webhook/BUILD|Makefile):[0-9]+", text)))[:12],
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def run(args):
    proc = subprocess.run(args, capture_output=True)
    if proc.returncode:
        RESULT["command_error"] = diagnostic(args, proc)
        raise CheckFailure("command failed")
    return proc.stdout



def retained(name, sha256):
    data = (WORK / (name + ".zip")).read_bytes()
    check(hashlib.sha256(data).hexdigest() == sha256, "wrong prior ZIP")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        check(archive.namelist() == ["result.json"] and archive.getinfo("result.json").file_size < 16384,
              "unexpected prior artifact")
        return json.loads(archive.read("result.json"))


def verify():
    obs = RESULT["observations"]
    expected = {}

    def prior():
        first = retained("prior", "d667ba72b24cb96070a816b39d1910561c15ea96de423abfb968271ec6d09f4c")
        second = retained("completion", "41cb9390e08cffd2297d7d15eb354129a1d60efac5d7e9dcb7c910555ae4ca1f")
        check(first["status"] == second["status"] == "FAIL", "wrong retained status")
        check([f["case"] for f in first["failed"]] == ["manifest list"] and
              [f["case"] for f in second["failed"]] == ["archive and execution"] and
              second["combined"]["remaining_amd64"] == ["archive and execution"] and
              second["observations"]["help_exit"] == 2 and second["combined"]["arm64_archive_verified"],
              "wrong retained boundary")
        check(first["observations"]["source"] == second["observations"]["source"] == PRODUCT and
              run(["git", "rev-parse", "HEAD"]).decode().strip() == PRODUCT, "wrong source")
        for key in ("config", "elf_sha256"):
            expected[key] = first["observations"][key]
            check(second["observations"][key] == expected[key], "prior identity mismatch")
        check(platform.machine() == "x86_64", "wrong native platform")
        RESULT["retained"] = {"source": PRODUCT, "original": {"id": 13304085544, "status": first["status"]},
                              "completion": {"id": 13304303999, "status": second["status"]},
                              "combined": second["combined"]}
        obs.update(platform="linux/amd64", cache="accepted")

    if not probe("retained receipts", prior):
        return
    docker_config = WORK / "docker-config"
    docker_config.mkdir(mode=0o700)
    os.environ["DOCKER_CONFIG"] = str(docker_config)
    tag = "webhook-dev:qa"
    immutable = expected["config"]
    cwd = "/certs"

    def identity():
        startup = "--bazelrc=" + str(WORK / "remote.bazelrc")
        run(["make", "--no-print-directory", "DOCKER_BUILD=false", "STARTUP_OPTIONS=" + startup,
             "BAZEL_OPTIONS=--config=x86_64 -c opt --lockfile_mode=off", "WEBHOOK_IMAGE=" + tag, "webhook-build"])
        image = json.loads(run(["docker", "image", "inspect", tag]))[0]
        check(image["Id"] == immutable and image["Architecture"] == "amd64" and image["Os"] == "linux"
              and image["Config"]["Entrypoint"] == ["/webhook"], "wrong cached image")
        container = run(["docker", "create", "--pull=never", "--network=none", "--workdir", cwd,
                         immutable, "--help"]).decode().strip()
        try:
            data = run(["docker", "cp", container + ":/webhook", "-"])
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                entries = archive.getmembers()
                check(len(entries) == 1 and entries[0].isfile() and entries[0].mode & 0o111 and
                      Path(entries[0].name).name == "webhook", "unexpected container executable")
                binary = archive.extractfile(entries[0]).read()
            check(binary[:4] == b"\x7fELF" and int.from_bytes(binary[18:20], "little") == 62 and
                  hashlib.sha256(binary).hexdigest() == expected["elf_sha256"], "wrong container executable")
            obs.update(config=immutable, elf_sha256=expected["elf_sha256"], machine=62, working_dir=cwd)
        finally:
            run(["docker", "rm", container])

    if not probe("AMD64 image and ELF identity", identity):
        return

    def launch(directory, label):
        result = subprocess.run(["docker", "run", "--pull=never", "--rm", "--network=none", "--workdir", cwd,
                                 "--mount", "type=bind,source=" + str(directory) + ",target=/certs,readonly",
                                 immutable, "--help"], capture_output=True)
        console = result.stdout + result.stderr
        record = {"exit": result.returncode, "output_sha256": hashlib.sha256(console).hexdigest(),
                  "missing_ca_key": b"panic: unable to create certificates: open caKey.pem: no such file or directory" in console,
                  "injector_init": b"injector/certs.go:" in console,
                  "usage": b"Usage of /webhook:" in console,
                  "port_flag": b"-port " in console, "label_flag": b"-pod-namespace-labels " in console}
        obs[label] = record
        return record

    def empty():
        directory = WORK / "empty-certs"
        directory.mkdir()
        result = launch(directory, "empty")
        check(result["exit"] == 2 and result["missing_ca_key"] and not result["usage"],
              "missing certificate did not produce the expected startup error")

    def mounted():
        public = (WORK / "testcert.go").read_bytes()
        check(hashlib.sha256(public).hexdigest() ==
              "812c69f79fc71384de1d93fb1058ab20e5b58ab2860dea72167681048d14a12e", "wrong public fixture")
        cert = re.search(rb"var LocalhostCert = [^\n]*?\x60([^\x60]+)\x60", public)[1]
        key = re.search(rb"var LocalhostKey = [^\n]*?\x60([^\x60]+)\x60", public)[1].replace(b"TESTING KEY", b"PRIVATE KEY")
        directory = WORK / "mounted-certs"
        directory.mkdir(mode=0o700)
        for name, contents in [("caCert.pem", cert), ("serverCert.pem", cert),
                               ("caKey.pem", key), ("serverKey.pem", key)]:
            path = directory / name
            path.write_bytes(contents)
            path.chmod(0o400)
        result = launch(directory, "mounted")
        check(result["exit"] == 0 and result["usage"] and result["port_flag"] and result["label_flag"]
              and not result["missing_ca_key"], "mounted files did not permit CLI help")

    probe("empty certificate directory", empty)
    probe("mounted fixture help", mounted)


try:
    verify()
except Exception as error:
    RESULT["failed"].append({"case": "shared fixture setup", "reason": type(error).__name__})
finally:
    RESULT["execution_finished_at"] = now()
    RESULT["missing"] = [item for item in CASES if item not in RESULT["executed"]]
    RESULT["status"] = "PASS" if RESULT["passed"] == CASES and not RESULT["failed"] else "FAIL"
    RESULT["verified_at"] = now()
    (WORK / "result.json").write_text(json.dumps(RESULT, indent=2) + "\n")
raise SystemExit(0 if RESULT["status"] == "PASS" else 1)
