"""Exercise the product's Bazel and Make entry points on an isolated Linux worker."""

import datetime
import hashlib
import http.client
import io
import json
import os
import platform
import re
import socket
import subprocess
import tarfile
import time
from pathlib import Path


WORK = Path(os.environ["RUNNER_TEMP"]) / "webhook-oci"
PRODUCT = "0d54cc79f0155cc02ffd4a1a0c679579f52af9dd"
REPOSITORY = "gvisor/qualification/webhook"
TAG = "qa"
CASES = [
    "source", "short-name routing", "daemon-free build", "offline manifest",
    "docker load", "isolated execution", "private registry", "make push",
    "published manifest", "scratch filesystem",
]
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
    start("short-name routing")
    for source, host, path, tag in (
        ("webhook-dev:qa", "index.docker.io", "library/webhook-dev", TAG),
        ("team/webhook", "team", "webhook", "latest"),
        (ref, registry, REPOSITORY, TAG),
    ):
        rendered = run([*make, "-n", "webhook-push", "WEBHOOK_IMAGE=" + source]).decode()
        check(f'--registry "{host}" --repository "{path}" --tag "{tag}"' in rendered,
              "wrong push coordinates")
    start("daemon-free build")
    run([*bazel, "build", *options, "//webhook:image", "//webhook:webhook"])
    paths = run([*bazel, "cquery", *options, "--output=files", "//webhook:webhook"])
    files = [Path(line) for line in paths.decode().splitlines() if line]
    check(len(files) == 1 and files[0].is_file(), "missing Bazel binary")
    binary_hash = hashlib.sha256(files[0].read_bytes()).hexdigest()
    start("offline manifest")
    output = Path("test/kubernetes/gvisor-injection-admission-webhook.yaml")
    run([*make, "webhook-update", "WEBHOOK_IMAGE=" + ref])
    manifest = output.read_text()
    match = re.search(r"^\s*image: " + re.escape(ref) + r"@(sha256:[a-f0-9]{64})$", manifest, re.M)
    check(match, "rendered image missing exact tag and digest")
    digest = match[1]
    certs = run([*make, "--eval", 'certs-ref:;@echo "$(call remote_image,certs):$(call tag,certs)"',
                 "certs-ref"]).decode().strip()
    check(re.fullmatch(r"us-central1-docker\.pkg\.dev/gvisor-presubmit/"
                       r"gvisor-presubmit-images/certs_x86_64:[a-f0-9]{16}", certs), "wrong certs reference")
    template = Path(str(output) + ".in").read_text()
    check(manifest == template.replace("%WEBHOOK%", ref + "@" + digest).replace("%INIT%", certs),
          "deployment template changed")
    check(manifest.count("workingDir: /certs") == 2, "unexpected deployment working directories")
    obs.update(rendered=digest, certs=certs, registry_started_after_render=True)
    start("docker load")
    local = "webhook-dev:" + TAG
    run([*make, "webhook-build", "WEBHOOK_IMAGE=" + local])
    image = json.loads(run(["docker", "image", "inspect", local]))[0]
    check(image["Architecture"] == "amd64" and image["Os"] == "linux", "wrong loaded platform")
    obs["docker_config"] = image["Id"]
    start("isolated execution")
    # --help exits before the Kubernetes client or a listening server is constructed.
    proc = subprocess.run(["docker", "run", "--pull=never", "--rm", "--network=none",
                           "--workdir=/certs", local, "--help"], capture_output=True)
    help_text = proc.stdout + proc.stderr
    obs["help_exit"] = proc.returncode
    check(proc.returncode == 0 and b"-pod-namespace-labels" in help_text and b"-port" in help_text,
          "webhook help did not execute")
    start("private registry")
    cfg = WORK / "registry.yaml"
    cfg.write_text("version: 0.1\nlog:\n  level: error\nstorage:\n  filesystem:\n"
                   f"    rootdirectory: {WORK}/storage\nhttp:\n  addr: {registry}\n")

    def get(path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            conn.request("GET", path, headers={"Accept": "application/vnd.oci.image.manifest.v1+json"})
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
        start("make push")
        run([*make, "webhook-push", "WEBHOOK_IMAGE=" + ref])
        start("published manifest")
        prefix = "/v2/" + REPOSITORY + "/"
        status, header, raw = get(prefix + "manifests/" + TAG)
        check(status == 200 and header == digest and "sha256:" + hashlib.sha256(raw).hexdigest() == digest,
              "pushed tag does not match the deployment")
        check(get(prefix + "manifests/" + digest)[2] == raw, "immutable reference not retrievable")
        check(get(prefix + "manifests/latest")[0] == 404, "unexpected latest tag")
        check(json.loads(get(prefix + "tags/list")[2])["tags"] == [TAG], "unexpected tag set")
        check(json.loads(get("/v2/_catalog")[2])["repositories"] == [REPOSITORY], "unexpected repository")
        desc = json.loads(raw)
        check(desc["config"]["digest"] == image["Id"], "published and loaded configs differ")
        cfg = json.loads(get(prefix + "blobs/" + desc["config"]["digest"])[2])["config"]
        check(cfg["Entrypoint"] == ["/webhook"] and not cfg.get("Cmd") and cfg.get("User", "") in ("", "0")
              and cfg.get("WorkingDir", "") in ("", "/"), "unexpected OCI configuration")
        obs.update(pushed=digest, only_tag=TAG, only_repository=REPOSITORY,
                   entrypoint=cfg["Entrypoint"], user=cfg.get("User"), working_dir=cfg.get("WorkingDir"))
        start("scratch filesystem")
        check(len(desc["layers"]) == 1, "unexpected layer count")
        layer = desc["layers"][0]["digest"]
        status, _, data = get(prefix + "blobs/" + layer)
        check(status == 200 and "sha256:" + hashlib.sha256(data).hexdigest() == layer, "wrong layer bytes")
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            entries = [entry for entry in archive if not entry.isdir()]
            check(len(entries) == 1 and entries[0].name.removeprefix("./").lstrip("/") == "webhook",
                  "unexpected scratch contents")
            entry = entries[0]
            check(entry.isfile() and entry.mode & 0o111, "webhook is not an executable file")
            binary = archive.extractfile(entry).read()
            check(binary[:4] == b"\x7fELF" and int.from_bytes(binary[18:20], "little") == 62 and
                  hashlib.sha256(binary).hexdigest() == binary_hash, "wrong webhook binary")
            obs.update(layer=layer, elf_sha256=binary_hash, elf_machine=62, mode=oct(entry.mode))
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
