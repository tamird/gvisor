"""Retain the four-arm source-only embed contrast before interpreting it."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

import qualification_report as report


OWNERS = ["//test/rbe/nogo_embed_deps:direct_nogo",
          "//test/rbe/nogo_embed_deps:mixed_nogo"]
BUILD_OWNERS = ["//test/rbe/nogo_embed_deps:direct",
                "//test/rbe/nogo_embed_deps:mixed"]
ARMS = ["current", "future", "full", "fixed"]


def check_source(evidence: Path, phase: dict) -> Path:
    directory = evidence / ("invocation-" + phase["name"])
    context = json.loads((directory / "context.json").read_text())
    result = phase["result"]
    if (result["source"] != phase["source"]
            or context["expected_source"] != phase["source"]
            or (directory / "head.txt").read_text().strip() != phase["source"]
            or int((directory / "native-exit").read_text()) != result["native_exit"]
            or int((directory / "capture-exit").read_text()) != 0
            or (directory / "before.txt").read_text()
            or (directory / "after.txt").read_text()
            or (directory / "expected-targets.txt").read_text().splitlines() != phase["targets"]):
        raise ValueError(f"{phase['name']}: source/native/owner evidence differs")
    return directory


def check_green(evidence: Path, phase: dict, paths: set[Path]) -> dict:
    """Require two real uncached analyzer owners and exact native reports."""
    directory = check_source(evidence, phase)
    result = phase["result"]
    if result["status"] != "PASS" or result["native_exit"] != 0:
        raise ValueError(f"{phase['name']}: native result did not pass")
    attempts = result["executed"]
    if (len(attempts) != 2
            or {item["owner"]["label"] for item in attempts} != set(OWNERS)):
        raise ValueError(f"{phase['name']}: expected two actual owners")
    expected = set()
    for attempt in attempts:
        key = (attempt["owner"]["label"], attempt["owner"]["configuration"],
               attempt["run"], attempt["attempt"], attempt["shard"])
        if (key in expected or key[2:] != (1, 1, 1)
                or attempt["status"] != "PASSED" or attempt["cached_locally"]
                or attempt["execution_info"].get("cachedRemotely", False)
                or attempt["execution_info"].get("strategy") != "local"):
            raise ValueError(f"{phase['name']}: repeated, cached or failed attempt")
        expected.add(key)
    manifest_path = report.manifest_path(directory / "events.jsonl")
    manifest = json.loads(manifest_path.read_text())
    _, _, errors = report.retained_reports(manifest_path)
    if errors:
        raise ValueError(f"{phase['name']}: invalid retained reports: {errors}")
    bodies = {key: {} for key in expected}
    for row in manifest["reports"]:
        identity = row["identity"]
        key = (identity["label"], identity["configuration"]["id"],
               identity["run"], identity["attempt"], identity["shard"])
        path = (directory / row["path"]).resolve()
        path.relative_to(directory.resolve())
        if key not in expected or path in paths or row["name"] in bodies[key]:
            raise ValueError(f"{phase['name']}: unexpected report identity/path")
        if report.file_record(path) != {"bytes": row["bytes"], "sha256": row["sha256"]}:
            raise ValueError(f"{phase['name']}: retained report digest differs")
        paths.add(path)
        bodies[key][row["name"]] = path.read_bytes()
    census = []
    for key, reports in bodies.items():
        if set(reports) != {"test.log", "test.xml"}:
            raise ValueError(f"{phase['name']}: expected one log/XML per owner")
        cases = list(ET.fromstring(reports["test.xml"]).iter("testcase"))
        if len(cases) != 1 or list(cases[0]):
            raise ValueError(f"{phase['name']}: expected one positive XML per owner")
        log = reports["test.log"].decode()
        if not all(f"{cpu}...PASS" in log for cpu in ("amd64", "arm64")):
            raise ValueError(f"{phase['name']}: missing actual analyzer CPU markers")
        census.append({"owner": key[0], "configuration": key[1],
                       "case": cases[0].attrib["name"], "analyzer_cpus": ["amd64", "arm64"]})
    return {"owners": sorted(census, key=lambda row: row["owner"]),
            "reports": manifest["reports"]}


def pairs(args: list[str], name: str) -> list[str]:
    """Read repeated native argv options without guessing shell syntax."""
    values = []
    for index, arg in enumerate(args):
        if arg == name:
            if index + 1 == len(args):
                raise ValueError(f"Missing native argument after {name}")
            values.append(args[index + 1])
    return values


def executed(row: dict) -> None:
    if (row['runner'] != 'remote' or row['cacheHit'] or row['exitCode']
            or row['status'] or not row['digest']['hash'] or not row['actualOutputs']):
        raise ValueError(f"No actual uncached successful action: {row['targetLabel']}")


def output_groups(directory: Path) -> list[dict]:
    groups = set()
    for line in (directory / 'events.jsonl').read_text().splitlines():
        event = json.loads(line)
        identity = event.get('id', {}).get('targetCompleted')
        if (identity and not identity.get('aspect')
                and identity['label'] in BUILD_OWNERS):
            owner = report.Owner.parse(identity)
            if not event['completed']['success'] or (owner, 'default') in groups:
                raise ValueError('Missing or duplicate successful build completion')
            groups.add((owner, 'default'))
    if len(groups) != 2 or {owner.label for owner, _ in groups} != set(BUILD_OWNERS):
        raise ValueError('Ordinary compile did not complete both configured owners')
    return report.advertised_build_outputs(directory / 'events.jsonl', groups)


def check_control(evidence: Path, phase: dict, paths: set[Path]) -> dict:
    directory = check_source(evidence, phase)
    result = phase['result']
    if result['status'] == 'PASS' and result['native_exit'] == 0:
        return {'classification': 'no_failure_observed', 'reports': check_green(evidence, phase, paths)}
    if result['status'] != 'FAIL' or result['native_exit'] == 0:
        raise ValueError(f"{phase['name']}: no definite native control outcome")
    records = []
    bodies = []
    for name in ('stdout.log', 'stderr.log'):
        path = directory / name
        bodies.append(path.read_text())
        records.append({'path': str(path.relative_to(evidence)), **report.file_record(path)})
    manifest_path = report.manifest_path(directory / 'events.jsonl')
    if manifest_path.exists():
        _, _, errors = report.retained_reports(manifest_path)
        if errors:
            raise ValueError(f"{phase['name']}: invalid control reports: {errors}")
        manifest = json.loads(manifest_path.read_text())
        identities = set()
        for row in manifest['reports']:
            ident = row['identity']
            key = (ident['label'], ident['configuration']['id'], ident['run'], ident['attempt'], ident['shard'], row['name'])
            path = (directory / row['path']).resolve()
            path.relative_to(directory.resolve())
            if (ident['label'] not in OWNERS or key in identities or path in paths
                    or report.file_record(path) != {'bytes': row['bytes'], 'sha256': row['sha256']}):
                raise ValueError('Invalid control report identity or digest')
            identities.add(key)
            paths.add(path)
            if row['name'] == 'test.log':
                bodies.append(path.read_text())
            records.append({'path': str(path.relative_to(evidence)), **report.file_record(path)})
    lines = sorted(set(line for body in bodies for line in body.splitlines()))
    dependency = 'gvisor.dev/gvisor/test/rbe/nogo_embed_deps/dependency'
    missing_import = [line for line in lines if dependency in line and
                      ('could not import' in line or 'unable to find' in line)]
    missing_declaration = [line for line in lines if 'undefined:' in line and
                           any(name in line for name in ('EmbeddedValue', 'NestedValue'))]
    if not missing_import and not missing_declaration:
        raise ValueError(f"{phase['name']}: native failure lacks relevant embed/import diagnostics")
    return {'classification': ('embedded_source_import_unresolved' if missing_import else
                               'embedded_declaration_unresolved'),
            'import_diagnostics': missing_import, 'declaration_diagnostics': missing_declaration,
            'native_status': result['status'], 'native_exit': result['native_exit'], 'records': records}


def check_action_semantics(evidence: Path, record: dict, config: dict) -> dict:
    """Bind ordinary compile and source-only Nogo import coverage separately."""
    directory = 'test/rbe/nogo_embed_deps/'
    expected_sources = {
        BUILD_OWNERS[0]: [directory + name for name in ('embedded.go', 'direct.go')],
        BUILD_OWNERS[1]: [directory + name for name in ('embedded.go', 'nested.go', 'compiled.go', 'mixed.go')],
    }
    dependency = 'gvisor.dev/gvisor/test/rbe/nogo_embed_deps/dependency'
    dependency_owner = '//test/rbe/nogo_embed_deps:dependency'
    result = {'ordinary_compiles': {}, 'fixed_analysis': {}, 'controls': {}}
    for arm in record['arms']:
        for build in arm['builds']:
            architecture = build['name'].rsplit('-', 1)[1]
            cpu = {'x86_64': 'amd64', 'aarch64': 'arm64'}[architecture]
            proof = json.loads((evidence / (architecture + '-' + build['name'] + '-image-execution.json')).read_text())
            if proof['status'] != 'PASS':
                raise ValueError('Ordinary compile action capture incomplete')
            rows = proof['image_actions']
            advertised = output_groups(check_source(evidence, build))
            selected = {}
            for label, sources in expected_sources.items():
                compiles = [row for row in rows if row['targetLabel'] == label and row['mnemonic'] == 'GoCompilePkg']
                if len(compiles) != 1:
                    raise ValueError(f'{build["name"]}/{label}: expected one compile action')
                row = compiles[0]
                executed(row)
                args = row['commandArgs']
                env = {item['name']: item['value'] for item in row['environmentVariables']}
                if (env.get('GOARCH') != cpu or env.get('GOOS') != 'linux'
                        or pairs(args, '-src') != sources
                        or len([value for value in pairs(args, '-arc') if value.startswith(dependency + '=' + dependency + '=')]) != 1):
                    raise ValueError(f'{build["name"]}/{label}: compile sources/CPU/import differ')
                outputs = {item['path']: (item['digest']['hash'], int(item['digest']['sizeBytes'])) for item in row['actualOutputs']}
                declared = [item for item in advertised if item['identity']['label'] == label]
                if not declared or any(outputs.get(item['name']) != (item['sha256'], item['bytes']) for item in declared):
                    raise ValueError('Native default outputs do not match the executed compile')
                selected[label] = {'action': row, 'default_outputs': declared}
            result['ordinary_compiles'][build['name']] = {'actions': selected, 'execution_log': proof['execution_log'], 'uncompressed': proof['uncompressed']}
        if arm['name'] != 'fixed':
            result['controls'][arm['name']] = check_control(evidence, arm['phase'], set())
    proof = json.loads((evidence / 'x86_64-fixed-nogo-image-execution.json').read_text())
    if proof['status'] != 'PASS':
        raise ValueError('Fixed analyzer action capture incomplete')
    rows = [row for row in proof['image_actions'] if row['mnemonic'] == 'GoStaticAnalysis' and 'check' in row['commandArgs']]
    if any(row['targetLabel'] in ('//test/rbe/nogo_embed_deps:embedded', '//test/rbe/nogo_embed_deps:nested') for row in rows):
        raise ValueError('Source-only carrier incorrectly acquired analyzer ownership')
    for label, sources in expected_sources.items():
        for cpu in ('amd64', 'arm64'):
            selected = [row for row in rows if row['targetLabel'] == label and '-GOARCH=' + cpu in row['commandArgs']]
            if len(selected) != 1:
                raise ValueError(f'{label}/{cpu}: expected one actual analyzer')
            row = selected[0]
            executed(row)
            args = row['commandArgs']
            if [arg for arg in args if arg.endswith('.go')] != sources:
                raise ValueError(f'{label}/{cpu}: analyzer source membership/order differs')
            imports = [arg.removeprefix('-import=' + dependency + '=') for arg in args if arg.startswith('-import=' + dependency + '=')]
            dep_rows = [item for item in rows if item['targetLabel'] == dependency_owner and '-GOARCH=' + cpu in item['commandArgs']]
            if len(imports) != 1 or len(dep_rows) != 1:
                raise ValueError('Missing source-derived embedded dependency export')
            executed(dep_rows[0])
            exports = [item for item in dep_rows[0]['actualOutputs'] if item['path'].endswith('.export')]
            if len(exports) != 1 or imports != [exports[0]['path']]:
                raise ValueError('Analyzer import does not name the owning dependency export')
            result['fixed_analysis'][label + '/' + cpu] = {'action': row, 'dependency_action': dep_rows[0]}
    result['fixed_execution_log'] = proof['execution_log']
    result['fixed_uncompressed'] = proof['uncompressed']
    return result


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    arms = config["arms"]
    if (config.get("draft", True) or config["targets"] != OWNERS
            or config["build_targets"] != BUILD_OWNERS
            or [arm["name"] for arm in arms] != ARMS
            or len({arm["source"] for arm in arms}) != 4
            or any(not arm["matched_inputs"] for arm in arms)
            or report.source_set(config["expected_sources"]) != {arm["source"] for arm in arms}
            or config["source"] != arms[-1]["source"]):
        raise ValueError("Unbound or unexpected four-arm contrast contract")
    options = ["--compilation_mode=opt", *config["consumer_options"],
               "--lockfile_mode=error", "--remote_download_outputs=all", "--noannounce_rc"]
    selected = ["--strategy=TestRunner=local", "--nocache_test_results", "--runs_per_test=1",
                "--flaky_test_attempts=1", "--test_output=all", "--test_timeout=300",
                "--test_sharding_strategy=disabled", "--test_tag_filters=", "--keep_going"]
    record = {"contract_status": "INCOMPLETE", "source": config["source"],
              "expected_sources": config["expected_sources"], "arms": [], "restored": False,
              "scope": "All native exits are preserved; control diagnostics remain uninterpreted until the owner semantic proof."}
    try:
        formatter = invoke("goimports", ["run", "--config=x86_64", *options, "--run_in_cwd",
                           "@org_golang_x_tools//cmd/goimports", "--", "-format-only", "-d",
                           *config["go_sources"]], "command", targets=[])
        record["formatter"] = formatter
        for arm in arms:
            source = arm["source"]
            subprocess.run(["git", "checkout", "--detach", source], check=True, timeout=30)
            if (subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != source
                    or subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"])):
                raise ValueError("Source switch did not reach a clean arm")
            for name, expected in arm["matched_inputs"].items():
                path = Path(name)
                body = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
                if hashlib.sha256(body).hexdigest() != expected:
                    raise ValueError(f"{arm['name']}: source input differs: {name}")
            arm_options = ["--platform_suffix=nogo-embed-" + arm["name"]]
            arm_record = {"name": arm["name"], "source": source,
                          "product": arm["product"], "builds": []}
            record["arms"].append(arm_record)
            for architecture in ("x86_64", "aarch64"):
                name = arm["name"] + "-build-" + architecture
                result = invoke(name, ["build", "--config=" + architecture, *options, *arm_options,
                                       "--keep_going", "--modify_execution_info=^GoCompilePkg$=+no-remote-cache",
                                       "--execution_log_json_file=" + str(private / (name + "-execution.json")),
                                       *BUILD_OWNERS],
                                "build", targets=BUILD_OWNERS, source=source)
                arm_record["builds"].append({"name": name, "source": source,
                                             "targets": BUILD_OWNERS, "result": result})
            name = arm["name"] + "-nogo"
            extra = (["--execution_log_json_file=" + str(private / (name + "-execution.json")),
                      "--modify_execution_info=^GoStaticAnalysis$=+no-remote-cache"]
                     if arm["name"] == "fixed" else [])
            result = invoke(name, ["test", "--config=x86_64", *options, *arm_options, *selected, *extra, *OWNERS],
                            "test", targets=OWNERS, source=source)
            arm_record["phase"] = {"name": name, "source": source,
                                   "targets": OWNERS, "result": result}
        # All twelve native arm commands return before status, report or
        # diagnostic interpretation. The shared driver preserves every exit.
        observed = {path.parent.name.removeprefix("invocation-"):
                    json.loads(path.read_text())["source"]
                    for path in evidence.glob("invocation-*/verdict.json")}
        if observed != config["expected_phase_sources"]:
            raise ValueError("Actual phase/source mapping differs from the plan")
        capture_errors = []
        for arm in record["arms"]:
            for build in arm["builds"]:
                name = build["name"]
                architecture = name.rsplit("-", 1)[1]
                try:
                    record_execution(private / (name + "-execution.json"), architecture, name)
                except (OSError, ValueError, RuntimeError) as error:
                    capture_errors.append(f"{name}: {error}")
        try:
            record_execution(private / "fixed-nogo-execution.json", "x86_64", "fixed-nogo")
        except (OSError, ValueError, RuntimeError) as error:
            capture_errors.append(f"fixed-nogo: {error}")
        if capture_errors:
            raise ValueError("; ".join(capture_errors))
        if (formatter["status"] != "PASS" or formatter["native_exit"] != 0
                or (evidence / "invocation-goimports/stdout.log").read_bytes()):
            raise ValueError("Canonical Go import formatting differs or failed")
        paths = set()
        for arm in record["arms"]:
            for build in arm["builds"]:
                check_source(evidence, build)
                if build["result"]["status"] != "PASS" or build["result"]["native_exit"] != 0:
                    raise ValueError(f"{build['name']}: ordinary compile did not pass")
            phase = arm["phase"]
            check_source(evidence, phase)
            if arm["name"] == "fixed":
                phase["checked"] = check_green(evidence, phase, paths)
            else:
                phase["uninterpreted_control"] = {
                    "status": phase["result"]["status"],
                    "native_exit": phase["result"]["native_exit"],
                }
        record["semantic_proof"] = check_action_semantics(evidence, record, config)
        record["contract_status"] = "PASS"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        record.update(contract_status="FAIL", error=str(error))
        raise
    finally:
        try:
            subprocess.run(["git", "checkout", "--detach", config["source"]], check=True, timeout=30)
            if (subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != config["source"]
                    or subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"])):
                raise ValueError("Failed to restore initial qualification source")
            record["restored"] = True
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            record.update(contract_status="FAIL", restoration_error=str(error))
            raise
        finally:
            report.write_json(evidence / "nogo-embed-deps-contrast.json", record)
