"""Run the checked-in Buildkite selection and compare the materialized packages."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
import textwrap

BASE_TAG = "release-20261101.0"
TAG = "release-20261112.2"
DIST = "2026.11.12.2"
OUT = Path(os.environ["RUNNER_TEMP"]) / "release-identity" / "public"
OPTIONS = ["--lockfile_mode=off", "--jobs=4", "--noshow_progress", "-c", "opt"]
STAMP = ["--config=release_version", "--embed_label=" + TAG]
FILES = {"runsc.deb", "gvisor.tar.bz2", "gvisor.tar.zstd"}
ARCHES = {"x86_64": ("amd64", 62), "aarch64": ("arm64", 183)}

def run(*args, **kwargs):
    result = subprocess.run(args, check=True, **kwargs)
    return result.stdout

def output(*args):
    return run(*args, text=True, stdout=subprocess.PIPE).strip()

def save(name, data):
    (OUT / (name + ".json")).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

def digest(path, algorithm="sha256"):
    with path.open("rb") as stream:
        value = hashlib.new(algorithm)
        while part := stream.read(1024 * 1024):
            value.update(part)
    return value.hexdigest()

def script(path):
    # Buildkite only expands the escaped dollars before handing this command
    # list to Bash. Extract the Release step, including the promotion command.
    text = path.read_text().split('label: ":ship: Release"', 1)[1]
    commands = text.split('    commands:\n', 1)[1].split('    agents:\n', 1)[0].splitlines()
    result = []
    for line in commands:
        if line.startswith("        "):
            result.append(line[8:])
        elif line.startswith("      - ") and line.strip() != "- |":
            result.append(line[8:])
    return "\n".join(result).replace("$$", "$")

def commands():
    release = script(Path(".buildkite/release.yaml"))
    return release.split("./.buildkite/scripts/release/tag.sh stage\n", 1)[1].split("# Sign the native artifacts above", 1)[0]

def promotion():
    results = []
    for version, yaml in [("prior", OUT.parent / "previous.yaml"),
                          ("revised", Path(".buildkite/release.yaml"))]:
        root = OUT.parent / ("promotion-" + version)
        for sub in ("bin", "tools", "repo", ".buildkite/scripts/release", "artifacts/python"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        shutil.copy2("tools/make_python_release.sh", root / "tools")
        def executable(path, body):
            f = root / path
            f.write_text("#!/bin/bash\nset -eu\n" + textwrap.dedent(body))
            f.chmod(0o755)
        executable(".buildkite/scripts/release/tag.sh", 'echo "tag $*" >> "$RECORD"\n')
        executable("bin/make", """
            echo "make $*" >> "$RECORD"
            if [[ "$1" == artifacts-python ]]; then
              [[ "${FAIL_PYTHON:-false}" == false ]] || exit 19
              touch artifacts/python/gvisor-2026.11.12.7-py3-none-any.whl
            fi
        """)
        executable("bin/gcloud", """
            printf 'gcloud %s\n' "$*" >> "$RECORD"
            if [[ "$*" == *".json"* ]]; then
              cp "$3" "$SAVED_MANIFEST"
            elif [[ "$*" != 'storage cp --recursive . gs://gvisor/releases/' ]]; then
              exit 33
            fi
        """)
        record = root / "calls"
        manifest = root / "manifest.json"
        env = dict(PATH=str(root / "bin") + ":/usr/bin:/bin", HOME=str(root), TMPDIR=str(root),
                   GIT_ALLOW_PROTOCOL="file", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
                   BUILDKITE_BUILD_NUMBER="101", RECORD=str(record), SAVED_MANIFEST=str(manifest))
        def git(*args):
            run("git", *args, cwd=root, env=env)
        def commit():
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@invalid", "commit", "-q", "--allow-empty", "-m", "fixture")
        git("init", "-q")
        commit()
        git("tag", TAG)
        for kind, staging, nightly in [("tagged", TAG+"-staging", False),
                                       ("tagged-nightly", "", True),
                                       ("master", "", False), ("nightly", "", True),
                                       ("failed-dist", TAG+"-staging", False)]:
            if kind == "master":
                commit()
            if kind == "failed-dist":
                git("checkout", "-q", "--detach", TAG)
            record.write_text("")
            manifest.unlink(missing_ok=True)
            result = subprocess.run(["bash", "-eu", "-c", script(yaml)], cwd=root,
                                    env=dict(env, BUILDKITE_TAG=staging,
                                             RELEASE_NIGHTLY=str(nightly).lower(),
                                             FAIL_PYTHON=str(kind=="failed-dist").lower()))
            calls = record.read_text().splitlines()
            promote = json.loads(manifest.read_text()) if manifest.exists() else None
            stale = (root/"artifacts/python/gvisor-2026.11.12.7-py3-none-any.whl").exists()
            results.append(dict(source=version, invocation=kind, commands=calls, manifest=promote,
                                wheel_present=stale, status=result.returncode))
            save("recorded-promotion", results)
            assert stale and (result.returncode == 19 if kind=="failed-dist" else result.returncode == 0), calls
            if kind == "failed-dist":
                assert not any(x.startswith(("tag publish", "gcloud")) for x in calls)
                continue
            wants = kind == "tagged"
            assert ("make artifacts-python" in calls) == wants
            assert (promote is not None) == (wants or version=="prior"), calls
            assert calls.index("tag publish") > calls.index("gcloud storage cp --recursive . gs://gvisor/releases/")
            if promote:
                assert calls[-1].startswith("gcloud storage cp") and promote["publish_all"] is False
                assert promote["publishing_groups"][0]["packages"] == [{"name":"gvisor","version":"2026.11.12.7"}]
            if version=="revised":
                sig = next(x for x in calls if x.startswith("make release-prebuilt "))
                assert sig.endswith("PYTHON_RELEASE_DEST=" + ("artifacts/python" if wants else "")), sig

def selection():
    run("git", "tag", BASE_TAG, "HEAD^")
    source = commands()
    previous = output("git", "describe", "--always", "--tags", "--exclude", "release-*-staging", "--abbrev=12", "--dirty")
    assert previous.startswith(BASE_TAG + "-1-g")
    records = []
    for name, staging, nightly, expected, wants_dist in [
        ("master", "", "false", previous, False),
        ("nightly", "", "true", previous, False),
        ("staging", TAG + "-staging", "false", TAG, True),
        ("tagged-nightly", "", "true", TAG, False),
    ]:
        if name == "staging":
            # tag.sh stage installs this verified ref on its clean working tree.
            run("git", "tag", TAG)
        env = dict(os.environ, BUILDKITE_TAG=staging, RELEASE_NIGHTLY=nightly)
        observed = run("bash", "-eu", "-c", 'make() { printf "%s\\n" "$@"; };\n' + source,
                       env=env, text=True, stdout=subprocess.PIPE).splitlines()
        assert observed == ["release-artifacts", "RELEASE_VERSION=" + expected,
                            "BAZEL_OPTIONS=--compilation_mode=opt"] + (["artifacts-python"] if wants_dist else []), observed
        records.append(dict(build=name, native=expected, python=wants_dist, argv=observed))
    # Check the actual signing recipe with prebuilt artifacts. The dummy key
    # is a prerequisite for the dry run; generated keys and uploads are unused.
    key = OUT.parent / "not-a-key"
    key.touch()
    args = ["make", "-n", "DOCKER_BUILD=false", "RELEASE_ARTIFACTS=custom-artifacts", "RELEASE_KEY=" + str(key)]
    sign = output(*args, "release-prebuilt")
    build = output(*args, "release")
    assert "//debian:release-archives" in build and "//debian:release-archives" not in sign
    assert '"custom-artifacts"' in sign and '"custom-artifacts"' in build
    assert "tools/make_release.sh" in sign and " bazel " not in sign and "artifacts-all" not in sign, sign
    save("production-selection", dict(cases=records, signing_recipe=sign.splitlines(), source_recipe=build.splitlines()))
    assert 'release-inputs' in Path(".buildkite/pipeline.yaml").read_text()
    # Use host mode of the same Makefile on the disposable runner. Its Bazel
    # and both GNU cross tools are provided in the previous step.
    gcloud = OUT.parent / "bin/gcloud"
    gcloud.write_text('#!/bin/sh\n[ "$1 $2 $3" = "artifacts versions list" ] || exit 41\n')
    gcloud.chmod(0o755)
    env = dict(os.environ, BUILDKITE_TAG=TAG + "-staging", RELEASE_NIGHTLY="false",
               MAKEFLAGS="DOCKER_BUILD=false",
               BAZEL_REMOTE_CACHE="--jobs=4 --lockfile_mode=off --noshow_progress")
    run("bash", "-eu", "-c", source, env=env)

def stage():
    wheels = list(Path("artifacts/python").glob("gvisor-*.whl"))
    assert len(wheels) == 1
    dist = wheels[0].name.split("-")[1]
    run("bazel", "build", *OPTIONS, *STAMP, "--remote_download_outputs=toplevel",
        "--//sandboxexec/sandbox/python:dist_version=" + dist, "//debian:release-inputs")
    root = Path("bazel-bin/debian/release-inputs").resolve(strict=True)
    assert root.is_dir()
    def deb_members(path):
        return {opt: hashlib.sha256(run("dpkg-deb", opt, str(path), stdout=subprocess.PIPE)).hexdigest()
                for opt in ("--fsys-tarfile", "--ctrl-tarfile")}

    rows = {}
    result = dict(staged=rows, python=[], mismatches=[])
    expected = {f"{arch}/{f}" for arch in ARCHES for f in FILES}
    assert set((root / "inputs").read_bytes().decode().rstrip("\0").split("\0")) == expected
    for relative in sorted(expected):
        path = root / relative
        staged = digest(path)
        production = Path("artifacts", relative)
        production_sha = digest(production)
        rows[relative] = dict(sha256=staged, size=path.stat().st_size, production_sha256=production_sha)
        if production_sha != staged:
            result["mismatches"].append("production/" + relative)
            if path.suffix == ".deb":
                rows[relative]["production_members"] = deb_members(production)
                rows[relative]["staged_members"] = deb_members(path)
        save("package-comparison",result)
        if path.suffix != ".deb":
            checkfile = path.with_name(path.name + ".sha512").read_text().strip()
            assert checkfile == digest(path, "sha512") + "  " + path.name, (relative,checkfile)
        else:
            deb_arch, machine = ARCHES[path.parent.name]
            assert output("dpkg-deb", "-f", str(path), "Architecture") == deb_arch
            assert output("dpkg-deb", "-f", str(path), "Version") == TAG.removeprefix("release-")
            rows[relative]["debian"] = [deb_arch, TAG.removeprefix("release-")]
            extract = OUT.parent / ("installed-" + deb_arch)
            run("dpkg-deb", "-x", str(path), str(extract))
            cli = extract / "usr/bin/runsc"
            with cli.open("rb") as reader:
                header = reader.read(20)
            assert header[:4] == b"\x7fELF" and int.from_bytes(header[18:20], "little") == machine
            rows[relative]["elf_machine"] = machine
            if deb_arch == "amd64":
                version = output(str(cli), "--version")
                assert TAG in version, version
                rows[relative]["runsc"] = version
    for pkg in sorted((root / "python").iterdir()):
        if pkg.name.endswith((".whl", ".tar.gz")):
            assert (Path("artifacts/python") / pkg.name).exists() and dist in pkg.name
            checkfile = pkg.with_name(pkg.name + ".sha512").read_text().strip()
            assert checkfile == digest(pkg,"sha512") + "  " + pkg.name
            assert digest(pkg) == digest(Path("artifacts/python") / pkg.name), pkg.name
            result["python"].append(pkg.name)
    assert len(result["python"]) == 2, result["python"]
    save("package-comparison",result)
    for arch in ARCHES:
        opts = [*OPTIONS, *STAMP, "--config=" + arch]
        run("bazel", "build", *opts, "//debian:release-packages")
        paths = output("bazel", "cquery", *opts, "--output=files", "//debian:release-packages").splitlines()
        assert {Path(x).name for x in paths} == FILES, paths
        for name in paths:
            direct = Path(name)
            key = arch + "/" + direct.name
            row = rows[key]
            row["direct_sha256"] = digest(direct)
            row["identical"] = row["sha256"] == row["direct_sha256"]
            if not row["identical"]:
                result["mismatches"].append(key)
                if direct.suffix == ".deb":
                    row["direct_members"],row["staged_members"] = deb_members(direct),deb_members(root / key)
        save("package-comparison", result)
    assert not result["mismatches"], result["mismatches"]

OUT.mkdir(parents=True, exist_ok=True)
{"promotion": promotion, "selection": selection, "stage": stage}[sys.argv[1]]()
