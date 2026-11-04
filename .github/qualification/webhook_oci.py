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
import zipfile
from pathlib import Path


WORK = Path(os.environ["RUNNER_TEMP"]) / "webhook-oci"
PRODUCT = "0bdc5723e11795c6063c95d36fba0c70817eacbc"
ORIGINAL = "0d54cc79f0155cc02ffd4a1a0c679579f52af9dd"
REPOSITORY = "gvisor/qualification/webhook"
TAG = "qa"
CASES = ["retained receipt", "short name", "registry fixture", "manifest list",
         "render failure", "OCI descriptor", "archive and execution", "ARM64 archive"]
REMAINING = {"short name", "manifest list", "render failure", "OCI descriptor", "archive and execution"}
CURRENT = ""
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


def check_layer(data, want_hash, machine):
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        files = [item for item in archive if not item.isdir()]
        check(len(files) == 1 and files[0].name.removeprefix("./").lstrip("/") == "webhook",
              "unexpected scratch contents")
        item = files[0]
        check(item.isfile() and item.mode & 0o111, "nonexecutable webhook")
        binary = archive.extractfile(item).read()
        check(binary[:4] == b"\x7fELF" and int.from_bytes(binary[18:20], "little") == machine and
              hashlib.sha256(binary).hexdigest() == want_hash, "wrong webhook ELF")
        return oct(item.mode)


def verify():
    obs = RESULT["observations"]
    prior = {}
    evidence = WORK / "prior.zip"

    def retained():
        check(hashlib.sha256(evidence.read_bytes()).hexdigest() ==
              "d667ba72b24cb96070a816b39d1910561c15ea96de423abfb968271ec6d09f4c", "wrong original ZIP")
        with zipfile.ZipFile(evidence) as archive:
            check(archive.namelist() == ["result.json"] and archive.getinfo("result.json").file_size < 16384,
                  "unexpected original artifact")
            original = json.loads(archive.read("result.json"))
        check(original["status"] == "FAIL" and [f["case"] for f in original["failed"]] == ["manifest list"] and
              set(original["expected"]) - set(original["passed"]) == REMAINING, "wrong original disposition")
        prior.update(original["observations"])
        check(prior["source"] == PRODUCT and run(["git", "rev-parse", "HEAD"]).decode().strip() == PRODUCT,
              "wrong original source")
        check(platform.machine() == "x86_64" and prior["native"] == "linux/amd64", "wrong coordinator")
        RESULT["retained"] = {"run": 41791814353, "artifact": 13304085544, "status": "FAIL",
                              "passed": original["passed"], "failed": original["failed"], "missing": original["missing"]}
        obs.update(source=PRODUCT, native="linux/amd64", remote_cache="accepted", config=prior["config"],
                   elf_sha256=prior["elf_sha256"])

    if not probe("retained receipt", retained):
        return
    docker_config = WORK / "docker-config"
    docker_config.mkdir(mode=0o700)
    os.environ["DOCKER_CONFIG"] = str(docker_config)
    startup = "--bazelrc=" + str(WORK / "remote.bazelrc")
    make = ["make", "--no-print-directory", "DOCKER_BUILD=false", "STARTUP_OPTIONS=" + startup,
            "BAZEL_OPTIONS=--config=x86_64 -c opt --lockfile_mode=off"]
    bazel = ["bazel", startup]
    local = "webhook-dev:qa"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    registry = f"127.0.0.1:{port}"
    reference = f"{registry}/{REPOSITORY}"
    ref = reference + ":" + TAG

    def short_name():
        run([*make, "WEBHOOK_IMAGE=" + local, "webhook-build"])
        image = json.loads(run(["docker", "image", "inspect", local]))[0]
        check(image["Id"] == prior["config"], "short name resolved another image")
        obs["short_name_config"] = image["Id"]

    short = probe("short name", short_name)
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

    list_type = "application/vnd.docker.distribution.manifest.list.v2+json"
    index_type = "application/vnd.oci.image.index.v1+json"

    def request(path, payload=None, media_type=None, expected_media=None):
        conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=30, context=context)
        try:
            headers = {"Accept": ", ".join([oci_type, docker_type, list_type, index_type])}
            if payload is not None:
                headers["Content-Type"] = media_type or oci_type
            conn.request("GET" if payload is None else "PUT", path, body=payload, headers=headers)
            response = conn.getresponse()
            if expected_media:
                check(response.getheader("Content-Type", "").split(";", 1)[0] == expected_media,
                      "registry returned the wrong media type")
            return response.status, response.getheader("Docker-Content-Digest"), response.read()
        finally:
            conn.close()

    proc = subprocess.Popen([str(WORK / "package/usr/bin/docker-registry"), "serve", str(cfg)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    seed = {}
    output = Path("test/kubernetes/gvisor-injection-admission-webhook.yaml")
    source_template = Path(str(output) + ".in")
    template = source_template.read_text()
    sentinel = "retained prior deployment\n"
    output.write_text(sentinel)
    update = [*make, "webhook-update"]

    def seed_registry():
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
        if short:
            run(["docker", "tag", local, ref])
        else:
            # A short-name failure must not hide the independent registry checks.
            run([*make, "WEBHOOK_IMAGE=" + ref, "webhook-build"])
        run(["docker", "push", ref])
        status, digest, raw = request(prefix + "manifests/" + TAG)
        check(status == 200 and digest == manifest_ref(raw) == prior["published"], "wrong warm fixture manifest")
        desc = json.loads(raw)
        check(desc["config"]["digest"] == prior["config"], "wrong warm fixture config")
        seed.update(desc=desc, raw=raw, digest=digest)
        obs.update(reseeded=digest, registry_tls=True)

    def manifest_list():
        descriptor = {"mediaType": seed["desc"]["mediaType"], "digest": seed["digest"],
                      "size": len(seed["raw"]), "platform": {"os": "linux", "architecture": "amd64"}}
        document = {"schemaVersion": 2, "mediaType": list_type, "manifests": [descriptor]}
        raw = json.dumps(document, separators=(",", ":")).encode()
        path = prefix + "manifests/list-control"
        status, uploaded, _ = request(path, raw, list_type)
        check(status in (201, 202) and uploaded == manifest_ref(raw), "list fixture publication failed")
        status, returned, actual = request(path, expected_media=list_type)
        got = json.loads(actual)
        check(status == 200 and returned == uploaded == manifest_ref(actual) and
              got["mediaType"] == list_type and got["manifests"] == [descriptor], "wrong list fixture")
        obs["list_fixture"] = {"digest": returned, "media_type": got["mediaType"], "http_content_type": list_type, "child": descriptor}
        obs["list_exit"] = must_reject([*update, "WEBHOOK_IMAGE=" + reference + ":list-control"], b"got a manifest list")
        check(output.read_text() == sentinel, "manifest list changed YAML")

    def render_failure():
        output.write_text(sentinel)
        mode = source_template.stat().st_mode
        try:
            check(os.geteuid() != 0, "expected nonroot renderer")
            source_template.chmod(0)
            obs["sed_exit"] = must_reject([*update, "WEBHOOK_IMAGE=" + ref], b"Permission denied")
        finally:
            source_template.chmod(mode)
        check(output.read_text() == sentinel and not list(output.parent.glob(output.name + ".??????")),
              "failed render changed YAML or left temporary file")

    def oci_descriptor():
        desc = seed["desc"]
        oci = dict(desc, mediaType=oci_type,
                   config=dict(desc["config"], mediaType="application/vnd.oci.image.config.v1+json"))
        oci["layers"] = [dict(layer, mediaType="application/vnd.oci.image.layer.v1.tar+gzip") for layer in desc["layers"]]
        raw = json.dumps(oci, separators=(",", ":")).encode()
        path = prefix + "manifests/oci-control"
        status, digest, _ = request(path, raw, oci_type)
        check(status in (201, 202) and digest == manifest_ref(raw), "OCI fixture publication failed")
        status, returned, actual = request(path, expected_media=oci_type)
        check(status == 200 and returned == digest and actual == raw, "wrong OCI fixture")
        oci_ref = reference + ":oci-control"
        run([*update, "WEBHOOK_IMAGE=" + oci_ref])
        check(output.read_text() == template.replace("%INIT%", prior["certs"]).replace("%WEBHOOK%", oci_ref + "@" + digest),
              "wrong OCI descriptor selection")
        obs["fixture_oci_digest"] = digest

    def archive_and_execution():
        image = json.loads(run(["docker", "image", "inspect", ref]))[0]
        check(image["Architecture"] == "amd64" and image["Os"] == "linux" and image["Id"] == prior["config"],
              "wrong loaded platform")
        runtime = image["Config"]
        check(runtime["Entrypoint"] == ["/webhook"] and not runtime.get("Cmd") and
              runtime.get("User", "") in ("", "0") and runtime.get("WorkingDir", "") in ("", "/"), "wrong image defaults")
        layers = seed["desc"]["layers"]
        check(len(layers) == 1, "unexpected layer count")
        layer = layers[0]["digest"]
        status, _, data = request(prefix + "blobs/" + layer)
        check(status == 200 and manifest_ref(data) == layer, "wrong layer bytes")
        mode = check_layer(data, prior["elf_sha256"], 62)
        help_run = subprocess.run(["docker", "run", "--pull=never", "--rm", "--network=none",
                                   "--workdir=/certs", ref, "--help"], capture_output=True)
        help_output = help_run.stdout + help_run.stderr
        obs["help_exit"] = help_run.returncode
        check(help_run.returncode == 0 and b"-pod-namespace-labels" in help_output and b"-port" in help_output,
              "webhook CLI did not execute")
        obs.update(elf_machine=62, layer=layer, mode=mode, entrypoint=runtime["Entrypoint"],
                   working_dir=runtime.get("WorkingDir"), user=runtime.get("User"))

    try:
        if probe("registry fixture", seed_registry):
            probe("manifest list", manifest_list)
            probe("render failure", render_failure)
            probe("OCI descriptor", oci_descriptor)
            probe("archive and execution", archive_and_execution)
    finally:
        proc.terminate()
        proc.wait(timeout=15)


def arm_archive():
    bazel = ["bazel", "--bazelrc=" + str(WORK / "remote.bazelrc")]
    flags = ["--config=aarch64", "-c", "opt", "--lockfile_mode=off"]
    run([*bazel, "build", *flags, "--remote_download_outputs=toplevel", "--output_groups=+oci_layout",
         "//webhook:image", "//webhook:webhook"])
    expr = 'providers(target)["OutputGroupInfo"].oci_layout.to_list()[0].path'
    query = [*bazel, "cquery", *flags]
    layout = Path(os.fsdecode(run([*query, "--output=starlark", "--starlark:expr=" + expr, "//webhook:image"]).strip()))
    binary = Path(os.fsdecode(run([*query, "--output=files", "//webhook:webhook"]).strip())).read_bytes()
    direct = hashlib.sha256(binary).hexdigest()
    check(binary[:4] == b"\x7fELF" and int.from_bytes(binary[18:20], "little") == 183, "wrong arm64 target binary")

    def blob(desc):
        digest = desc["digest"]
        check(re.fullmatch(r"sha256:[0-9a-f]{64}", digest), "invalid OCI layout descriptor")
        raw = (layout / "blobs/sha256" / digest[7:]).read_bytes()
        check(manifest_ref(raw) == digest and len(raw) == desc["size"], "wrong OCI layout blob")
        return raw

    manifests = json.loads((layout / "index.json").read_bytes())["manifests"]
    check(len(manifests) == 1, "unexpected arm image count")
    manifest = json.loads(blob(manifests[0]))
    config = json.loads(blob(manifest["config"]))
    check(config["architecture"] == "arm64" and config["os"] == "linux" and
          config["config"]["Entrypoint"] == ["/webhook"], "wrong arm image config")
    check(len(manifest["layers"]) == 1, "unexpected arm layer count")
    mode = check_layer(blob(manifest["layers"][0]), direct, 183)
    RESULT["observations"]["arm64"] = {"platform": "linux/arm64", "runtime_executed": False, "elf_machine": 183,
                                      "elf_sha256": direct, "manifest": manifests[0]["digest"],
                                      "config": manifest["config"]["digest"], "mode": mode}


try:
    verify()
except Exception as error:
    RESULT["failed"].append({"case": "shared fixture setup", "reason": type(error).__name__})
finally:
    if "retained receipt" in RESULT["passed"]:
        probe("ARM64 archive", arm_archive)
    RESULT["execution_finished_at"] = now()
    RESULT["missing"] = [item for item in CASES if item not in RESULT["executed"]]
    RESULT["status"] = "PASS" if RESULT["passed"] == CASES and not RESULT["failed"] else "FAIL"
    retained = RESULT.get("retained", {}).get("passed", [])
    RESULT["combined"] = {"retained_amd64": retained, "new_amd64": sorted(REMAINING.intersection(RESULT["passed"])),
                          "remaining_amd64": sorted(REMAINING - set(RESULT["passed"])),
                          "arm64_archive_verified": "ARM64 archive" in RESULT["passed"]}
    RESULT["verified_at"] = now()
    (WORK / "result.json").write_text(json.dumps(RESULT, indent=2) + "\n")
raise SystemExit(0 if RESULT["status"] == "PASS" else 1)
