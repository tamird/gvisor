"""Exercise the product's Bazel and Make entry points on an isolated Linux worker."""

import datetime
import hashlib
import http.client
import json
import os
import platform
import re
import socket
import subprocess
import time
from pathlib import Path


WORK = Path(os.environ["RUNNER_TEMP"]) / "webhook-oci"
PRODUCT = "0d54cc79f0155cc02ffd4a1a0c679579f52af9dd"
REPOSITORY = "gvisor/qualification/webhook"
TAG = "qa"
CASES = ["source", "declared digest", "docker load", "private registry", "host push",
         "published manifest", "published digest lookup"]
CURRENT = ""
RESULT = {"expected": CASES, "executed": [], "passed": [], "failed": [], "observations": {}}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def start(case):
    global CURRENT
    if CURRENT:
        RESULT["passed"].append(CURRENT)
    CURRENT = case
    RESULT["executed"].append(case)
    print(case, flush=True)


class CheckFailure(Exception):
    """A fixed diagnostic safe for publication."""


def check(condition, reason):
    if not condition:
        raise CheckFailure(reason)


def run(args):
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode:
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
        )
        RESULT["command_error"] = {
            "tool": args[0], "exit": proc.returncode,
            "signatures": [s for s in signatures if s.lower() in text.lower()],
            "locations": sorted(set(re.findall(r"(?:webhook/BUILD|Makefile):[0-9]+", text)))[:12],
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
        raise CheckFailure("command failed")
    return proc.stdout


def verify():
    obs = RESULT["observations"]
    docker_config = WORK / "docker-config"
    docker_config.mkdir(mode=0o700)
    os.environ["DOCKER_CONFIG"] = str(docker_config)
    start("source")
    check(run(["git", "rev-parse", "HEAD"]).decode().strip() == PRODUCT, "source mismatch")
    check(platform.machine() == "x86_64", "wrong native architecture")
    obs.update(source=PRODUCT, native="linux/amd64", remote_cache="accepted")
    startup = "--bazelrc=" + str(WORK / "remote.bazelrc")
    bazel = ["bazel", startup]
    options = ["--config=x86_64", "-c", "opt", "--lockfile_mode=off"]
    make = ["make", "--no-print-directory", "DOCKER_BUILD=false", "STARTUP_OPTIONS=" + startup,
            "BAZEL_OPTIONS=" + " ".join(options)]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    registry = f"127.0.0.1:{port}"
    ref = f"{registry}/{REPOSITORY}:{TAG}"
    start("declared digest")
    run([*bazel, "build", *options, "--remote_download_outputs=toplevel", "//webhook:image_digest"])
    files = run([*bazel, "cquery", *options, "--output=files", "//webhook:image_digest"]).splitlines()
    check(len(files) == 1, "expected one digest file")
    digest = Path(os.fsdecode(files[0])).read_text().strip()
    check(re.fullmatch(r"sha256:[a-f0-9]{64}", digest), "invalid declared digest")
    obs["declared_digest"] = digest
    start("docker load")
    run([*make, "webhook-build", "WEBHOOK_IMAGE=" + ref])
    image = json.loads(run(["docker", "image", "inspect", ref]))[0]
    check(image["Architecture"] == "amd64" and image["Os"] == "linux", "wrong loaded platform")
    obs["docker_config"] = image["Id"]
    version = run(["docker", "version", "--format", "{{.Server.Version}}"]).decode().strip()
    driver = run(["docker", "info", "--format", "{{.Driver}}"]).decode().strip()
    driver_status = json.loads(run(["docker", "info", "--format", "{{json .DriverStatus}}"]))
    check(re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", version), "unknown Docker version")
    obs.update(docker_version=version, driver=driver if driver in ("overlay2", "overlayfs", "vfs") else "other",
               containerd_snapshotter=any(row == ["driver-type", "io.containerd.snapshotter.v1"]
                                          for row in driver_status or []))
    start("private registry")
    cfg = WORK / "registry.yaml"
    cfg.write_text("version: 0.1\nlog:\n  level: error\nstorage:\n  filesystem:\n"
                   f"    rootdirectory: {WORK}/storage\nhttp:\n  addr: {registry}\n")

    def get(path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            conn.request("GET", path, headers={"Accept": "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"})
            response = conn.getresponse()
            return response.status, response.getheader("Docker-Content-Digest"), response.read()
        finally:
            conn.close()

    proc = subprocess.Popen([str(WORK / "package/usr/bin/docker-registry"), "serve", str(cfg)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            if proc.poll() is not None:
                raise CheckFailure("private registry exited")
            try:
                if get("/v2/")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.25)
        else:
            raise CheckFailure("private registry unavailable")
        start("host push")
        # These are the same two commands as the original Make target: build/load,
        # then invoke the current user's Docker CLI to publish exactly that name.
        run(["docker", "push", ref])
        start("published manifest")
        prefix = "/v2/" + REPOSITORY + "/"
        status, header, raw = get(prefix + "manifests/" + TAG)
        check(status == 200 and header == "sha256:" + hashlib.sha256(raw).hexdigest(),
              "invalid host registry response")
        check(get(prefix + "manifests/" + header)[2] == raw, "immutable reference not retrievable")
        check(get(prefix + "manifests/latest")[0] == 404, "unexpected latest tag")
        check(json.loads(get(prefix + "tags/list")[2])["tags"] == [TAG], "unexpected tag set")
        check(json.loads(get("/v2/_catalog")[2])["repositories"] == [REPOSITORY], "unexpected repository")
        desc = json.loads(raw)
        check(desc["config"]["digest"] == image["Id"], "host Docker pushed another image")
        check(len(desc["layers"]) == 1, "unexpected layer count")
        obs.update(host_pushed=header, host_matches_declared=(digest == header), tag=TAG,
                   media_type=desc.get("mediaType"), layer=desc["layers"][0]["digest"])
        start("published digest lookup")
        # The CLI's verbose inspect includes the actual registry descriptor.
        # Check a typed alternative for a renderer if this daemon converts OCI.
        inspect = ["docker", "manifest", "inspect", "--verbose"]
        try:
            native = json.loads(run([*inspect, ref]))
            obs["lookup_mode"] = "default"
        except CheckFailure:
            obs["loopback_default_error"] = RESULT.pop("command_error")
            native = json.loads(run([*inspect, "--insecure", ref]))
            obs["lookup_mode"] = "explicit loopback HTTP"
        check(native["Descriptor"]["digest"] == header, "Docker descriptor differs from registry")
        obs["lookup_digest"] = native["Descriptor"]["digest"]
    finally:
        proc.terminate()
        proc.wait(timeout=15)


try:
    verify()
    RESULT["passed"].append(CURRENT)
except Exception as error:
    # Library exceptions may contain paths or responses.
    reason = str(error) if isinstance(error, CheckFailure) else type(error).__name__
    RESULT["failed"].append({"case": CURRENT, "error_type": type(error).__name__,
                             "reason": reason})
finally:
    RESULT["execution_finished_at"] = now()
    RESULT["missing"] = [case for case in CASES if case not in RESULT["executed"]]
    RESULT["status"] = "PASS" if RESULT["passed"] == CASES and not RESULT["failed"] else "FAIL"
    RESULT["verified_at"] = now()
    (WORK / "result.json").write_text(json.dumps(RESULT, indent=2) + "\n")
raise SystemExit(0 if RESULT["status"] == "PASS" else 1)
