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
import ssl
import subprocess
import tarfile
import time
from pathlib import Path


WORK = Path(os.environ["RUNNER_TEMP"]) / "webhook-oci"
PRODUCT = "0bdc5723e11795c6063c95d36fba0c70817eacbc"
ORIGINAL = "0d54cc79f0155cc02ffd4a1a0c679579f52af9dd"
REPOSITORY = "gvisor/qualification/webhook"
TAG = "qa"
CASES = [
    "source", "original Make failure", "Bazel image", "TLS registry", "Make push",
    "matching deployment", "missing publication", "stale build", "manifest list",
    "render failure", "OCI descriptor",
    "archive and execution", "short name",
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
    return {
        "tool": args[0], "exit": proc.returncode,
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


def manifest_ref(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def must_reject(args, expected_reason):
    proc = subprocess.run(args, capture_output=True)
    text = proc.stdout + proc.stderr
    check(proc.returncode != 0, "negative control succeeded")
    if expected_reason not in text:
        RESULT["command_error"] = diagnostic(args, proc)
        raise CheckFailure("negative control failed at a different boundary")
    return proc.returncode


def verify():
    obs = RESULT["observations"]
    start("source")
    docker_config = WORK / "docker-config"
    docker_config.mkdir(mode=0o700)
    os.environ["DOCKER_CONFIG"] = str(docker_config)
    check(run(["git", "rev-parse", "HEAD"]).decode().strip() == PRODUCT, "source mismatch")
    check(platform.machine() == "x86_64", "wrong native architecture")
    obs.update(source=PRODUCT, native="linux/amd64", remote_cache="accepted")
    startup = "--bazelrc=" + str(WORK / "remote.bazelrc")
    bazel = ["bazel", startup]
    options = ["--config=x86_64", "-c", "opt", "--lockfile_mode=off"]
    make = ["make", "--no-print-directory", "DOCKER_BUILD=false", "STARTUP_OPTIONS=" + startup]
    base_options = "BAZEL_OPTIONS=" + " ".join(options)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    registry = f"127.0.0.1:{port}"
    reference = f"{registry}/{REPOSITORY}"
    ref = reference + ":" + TAG
    start("original Make failure")
    run(["git", "checkout", "--quiet", "--detach", ORIGINAL])
    old = [*make, base_options, "WEBHOOK_IMAGE=" + ref, "webhook-update"]
    expansion = run([*old, "-n"])
    check(b"digest=$((set -euo pipefail;" in expansion, "missing historical adjacent parentheses")
    proc = subprocess.run(old, capture_output=True)
    error = proc.stderr.lower()
    signatures = [text for text in ["syntax error", "invalid arithmetic operator", "arithmetic",
                                    "makefile:899"] if text.encode() in error]
    obs["original"] = {"source": ORIGINAL, "exit": proc.returncode, "signatures": signatures,
                       "adjacent_arithmetic_expansion": True}
    check(proc.returncode == 2 and "makefile:899" in signatures, "wrong historical Make boundary")
    run(["git", "checkout", "--quiet", "--detach", PRODUCT])
    start("Bazel image")
    run([*bazel, "build", *options, "--remote_download_outputs=toplevel",
         "//webhook:image", "//webhook:manifest", "//webhook:webhook"])

    def product_file(label, extra=()):
        outputs = run([*bazel, "cquery", *options, *extra, "--output=files", label]).splitlines()
        check(len(outputs) == 1, "wrong product output count")
        return Path(os.fsdecode(outputs[0])).read_bytes()

    declared_raw = product_file("//webhook:manifest")
    config = json.loads(declared_raw)["config"]["digest"]
    binary_hash = hashlib.sha256(product_file("//webhook:webhook")).hexdigest()
    obs.update(oci_digest=manifest_ref(declared_raw), config=config, elf_sha256=binary_hash)
    start("TLS registry")
    # This is Go's fixed public localhost test pair. Its SAN includes 127.0.0.1.
    # Only this fixture process gets the extra trust anchor.
    testcert = (WORK / "testcert.go").read_text()
    check(hashlib.sha256(testcert.encode()).hexdigest() ==
          "812c69f79fc71384de1d93fb1058ab20e5b58ab2860dea72167681048d14a12e", "wrong test fixture")
    cert = re.search(r"var LocalhostCert = [^\n]*?\x60([^\x60]+)\x60", testcert)[1].encode()
    key = re.search(r"var LocalhostKey = [^\n]*?\x60([^\x60]+)\x60", testcert)[1].encode()
    (WORK / "localhost.crt").write_bytes(cert)
    (WORK / "localhost.key").write_bytes(key.replace(b"TESTING KEY", b"PRIVATE KEY"))
    (WORK / "localhost.key").chmod(0o600)
    trust = WORK / "public-trust.pem"
    trust.write_bytes(Path("/etc/ssl/certs/ca-certificates.crt").read_bytes() + b"\n" + cert)
    os.environ["SSL_CERT_FILE"] = str(trust)
    context = ssl.create_default_context(cafile=str(trust))
    cfg = WORK / "registry.yaml"
    cfg.write_text("version: 0.1\nlog:\n  level: error\nstorage:\n  filesystem:\n"
                   f"    rootdirectory: {WORK}/storage\nhttp:\n  addr: {registry}\n  tls:\n"
                   f"    certificate: {WORK}/localhost.crt\n    key: {WORK}/localhost.key\n")
    oci_type = "application/vnd.oci.image.manifest.v1+json"
    docker_type = "application/vnd.docker.distribution.manifest.v2+json"
    prefix = "/v2/" + REPOSITORY + "/"

    def request(path, payload=None):
        conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=30, context=context)
        try:
            headers = {"Accept": oci_type + ", " + docker_type}
            if payload is not None:
                headers["Content-Type"] = oci_type
            conn.request("GET" if payload is None else "PUT", path, body=payload, headers=headers)
            response = conn.getresponse()
            return response.status, response.getheader("Docker-Content-Digest"), response.read()
        finally:
            conn.close()

    proc = subprocess.Popen([str(WORK / "package/usr/bin/docker-registry"), "serve", str(cfg)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            check(proc.poll() is None, "private TLS registry exited")
            try:
                if request("/v2/")[0] == 200:
                    break
            except ssl.SSLCertVerificationError as error:
                obs["tls_verify_code"] = error.verify_code
                raise CheckFailure("public test certificate verification failed") from error
            except OSError:
                pass
            time.sleep(0.25)
        else:
            raise CheckFailure("private TLS registry unavailable")
        start("Make push")
        run([*make, base_options, "WEBHOOK_IMAGE=" + ref, "webhook-push"])
        status, published, raw = request(prefix + "manifests/" + TAG)
        check(status == 200 and published == manifest_ref(raw), "wrong published digest")
        check(request(prefix + "manifests/" + published)[2] == raw, "unreachable pinned manifest")
        check(request(prefix + "manifests/latest")[0] == 404, "unexpected latest tag")
        check(json.loads(request(prefix + "tags/list")[2])["tags"] == [TAG], "unexpected tag set")
        check(json.loads(request("/v2/_catalog")[2])["repositories"] == [REPOSITORY], "unexpected repository")
        desc = json.loads(raw)
        image = json.loads(run(["docker", "image", "inspect", ref]))[0]
        check(desc["config"]["digest"] == image["Id"] == config, "wrong published image config")
        version = run(["docker", "version", "--format", "{{.Server.Version}}"]).decode().strip()
        check(re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", version), "unrecognized Docker version")
        obs.update(published=published, published_type=desc["mediaType"], tag=TAG, docker_version=version)
        start("matching deployment")
        output = Path("test/kubernetes/gvisor-injection-admission-webhook.yaml")
        template = Path(str(output) + ".in").read_text()
        certs = run([*make, "--eval", 'certs-ref:;@echo "$(call remote_image,certs):$(call tag,certs)"',
                     "certs-ref"]).decode().strip()
        check(re.fullmatch(r"us-central1-docker\.pkg\.dev/gvisor-presubmit/"
                           r"gvisor-presubmit-images/certs_x86_64:[a-f0-9]{16}", certs), "wrong certificate init image")
        update = [*make, base_options, "webhook-update"]
        run([*update, "WEBHOOK_IMAGE=" + ref])
        golden = template.replace("%INIT%", certs).replace("%WEBHOOK%", ref + "@" + published)
        check(output.read_text() == golden and golden.count("workingDir: /certs") == 2, "wrong deployment YAML")
        obs.update(certs=certs, matched_published=True, registry_tls=True)
        start("missing publication")
        obs["missing_exit"] = must_reject([*update, "WEBHOOK_IMAGE=" + reference + ":missing"], b"manifest")
        check(output.read_text() == golden, "missing tag changed YAML")
        start("stale build")
        extra = "--@rules_img//img/settings:additional_image_labels_file=@webhook_gnu//:stale-labels.json"
        obs["stale_exit"] = must_reject([*make, base_options + " " + extra,
                                         "WEBHOOK_IMAGE=" + ref, "webhook-update"], b"published image differs from this build")
        stale_config = json.loads(product_file("//webhook:manifest", [extra]))["config"]["digest"]
        check(stale_config != config and output.read_text() == golden, "stale tag changed YAML")
        obs["different_selected_config"] = stale_config
        start("manifest list")
        list_ref = reference + ":list-control"
        run(["docker", "manifest", "create", list_ref, ref])
        run(["docker", "manifest", "push", "--purge", list_ref])
        obs["list_exit"] = must_reject([*update, "WEBHOOK_IMAGE=" + list_ref], b"got a manifest list")
        check(output.read_text() == golden, "manifest list changed YAML")
        start("render failure")
        source_template = Path(str(output) + ".in")
        mode = source_template.stat().st_mode
        try:
            check(os.geteuid() != 0, "expected nonroot renderer")
            source_template.chmod(0)
            obs["sed_exit"] = must_reject([*update, "WEBHOOK_IMAGE=" + ref], b"Permission denied")
        finally:
            source_template.chmod(mode)
        check(output.read_text() == golden and not list(output.parent.glob(output.name + ".??????")),
              "failed render changed YAML or left temporary file")
        start("OCI descriptor")
        # Reuse the exact pushed config and compressed blobs; only the media types change.
        oci = dict(desc, mediaType=oci_type, config=dict(desc["config"], mediaType="application/vnd.oci.image.config.v1+json"))
        oci["layers"] = [dict(layer, mediaType="application/vnd.oci.image.layer.v1.tar+gzip") for layer in desc["layers"]]
        oci_raw = json.dumps(oci, separators=(",", ":")).encode()
        status, oci_digest, _ = request(prefix + "manifests/oci-control", oci_raw)
        check(status in (201, 202) and oci_digest == manifest_ref(oci_raw), "OCI fixture publication failed")
        oci_ref = reference + ":oci-control"
        run([*update, "WEBHOOK_IMAGE=" + oci_ref])
        check(output.read_text() == template.replace("%INIT%", certs).replace("%WEBHOOK%", oci_ref + "@" + oci_digest),
              "wrong OCI descriptor selection")
        obs["fixture_oci_digest"] = oci_digest
        start("archive and execution")
        check(image["Architecture"] == "amd64" and image["Os"] == "linux", "wrong loaded platform")
        runtime = image["Config"]
        check(runtime["Entrypoint"] == ["/webhook"] and not runtime.get("Cmd") and
              runtime.get("User", "") in ("", "0") and runtime.get("WorkingDir", "") in ("", "/"), "wrong image defaults")
        check(len(desc["layers"]) == 1, "unexpected layer count")
        layer = desc["layers"][0]["digest"]
        status, _, data = request(prefix + "blobs/" + layer)
        check(status == 200 and manifest_ref(data) == layer, "wrong layer bytes")
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            entries = [entry for entry in archive if not entry.isdir()]
            check(len(entries) == 1 and entries[0].name.removeprefix("./").lstrip("/") == "webhook",
                  "unexpected scratch contents")
            entry = entries[0]
            check(entry.isfile() and entry.mode & 0o111, "nonexecutable webhook")
            binary = archive.extractfile(entry).read()
            check(binary[:4] == b"\x7fELF" and int.from_bytes(binary[18:20], "little") == 62 and
                  hashlib.sha256(binary).hexdigest() == binary_hash, "wrong webhook ELF")
        help_run = subprocess.run(["docker", "run", "--pull=never", "--rm", "--network=none",
                                   "--workdir=/certs", ref, "--help"], capture_output=True)
        help_output = help_run.stdout + help_run.stderr
        obs["help_exit"] = help_run.returncode
        check(help_run.returncode == 0 and b"-pod-namespace-labels" in help_output and b"-port" in help_output,
              "webhook CLI did not execute")
        obs.update(elf_machine=62, layer=layer, mode=oct(entry.mode), entrypoint=runtime["Entrypoint"],
                   working_dir=runtime.get("WorkingDir"), user=runtime.get("User"), help_exit=help_run.returncode)
        start("short name")
        local = "webhook-dev:qa"
        run([*make, base_options, "WEBHOOK_IMAGE=" + local, "webhook-build"])
        short = json.loads(run(["docker", "image", "inspect", local]))[0]
        check(short["Id"] == config, "short name resolved another image")
    finally:
        proc.terminate()
        proc.wait(timeout=15)


try:
    verify()
    RESULT["passed"].append(CURRENT)
except Exception as error:
    reason = str(error) if isinstance(error, CheckFailure) else type(error).__name__
    RESULT["failed"].append({"case": CURRENT, "error_type": type(error).__name__, "reason": reason})
finally:
    RESULT["execution_finished_at"] = now()
    RESULT["missing"] = [case for case in CASES if case not in RESULT["executed"]]
    RESULT["status"] = "PASS" if RESULT["passed"] == CASES and not RESULT["failed"] else "FAIL"
    RESULT["verified_at"] = now()
    (WORK / "result.json").write_text(json.dumps(RESULT, indent=2) + "\n")
raise SystemExit(0 if RESULT["status"] == "PASS" else 1)
