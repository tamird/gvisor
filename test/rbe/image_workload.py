"""Exercise current netlink units and the ten multicast syscall regressions."""
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET

import qualification_report as report


UNITS = [
    "//pkg/sentry/socket/netstack:netstack_test",
    "//pkg/sentry/socket/netlink/route:route_test",
    "//pkg/tcpip:tcpip_test",
    "//pkg/tcpip/stack:stack_x_test",
]
ANALYZERS = [
    "//pkg/sentry/socket/netstack:netstack_nogo",
    "//pkg/sentry/socket/netlink/route:route_nogo",
    "//pkg/tcpip:tcpip_nogo",
    "//pkg/tcpip/stack:stack_nogo",
]
SYSCALLS = ["//test/syscalls:socket_netlink_route_test_" + suffix for suffix in (
    "native", "runsc_systrap_directfs", "runsc_systrap_shared",
)]
CASES = {
    "NetlinkRouteIpv4AndIpv6Tests/NetlinkRouteIpInvariantTest." + family + "/" + str(ip)
    for family in ("AddressMulticastGroup", "AddressPrefixRouteMulticastGroup",
                   "RouteMulticastGroup", "RouteMulticastGroupSameDestination",
                   "RouteMulticastGroupReplace")
    for ip in (0, 1)
}


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    if config["targets"] != UNITS + ANALYZERS + SYSCALLS:
        raise ValueError(f"Unexpected netlink target selection: {config['targets']}")
    record = {"source": config["source"], "product": config["product"], "status": "INCOMPLETE", "checks": []}
    try:
        formatted = invoke("goimports", [
            "run", "--config=x86_64", "--compilation_mode=opt", *config["consumer_options"],
            "--lockfile_mode=error", "--noannounce_rc", "--run_in_cwd",
            "@org_golang_x_tools//cmd/goimports", "--", "-format-only", "-d",
            "pkg/sentry/socket/netstack/stack.go",
        ], "command", targets=[])
        if formatted["status"] != "PASS" or (evidence / "invocation-goimports/stdout.log").read_bytes():
            raise RuntimeError("Canonical goimports reported a formatting difference")
        options = [
            "--config=x86_64", "--compilation_mode=opt", "--lockfile_mode=error",
            *config["consumer_options"], "--remote_download_outputs=all",
            "--noannounce_rc", "--keep_going", "--strategy=TestRunner=local",
            "--nocache_test_results", "--runs_per_test=1", "--flaky_test_attempts=1",
            "--test_output=all", "--test_sharding_strategy=disabled", "--test_timeout=600",
        ]
        phases = [
            ("netlink-units", UNITS, ["--test_env=GO_TEST_WRAP=1", "--test_env=GO_TEST_WRAP_TESTV=1"]),
            ("netlink-nogo", ANALYZERS, []),
        ]
        # GTEST_FILTER is inherited by the actual binary during discovery and
        # execution. The existing runner builds its exact selected-case argv.
        # Do not send Go test flags to the custom syscall runner.
        for suffix, owner in zip(("linux", "directfs", "shared"), SYSCALLS, strict=True):
            phases.append(("netlink-" + suffix, [owner], ["--test_env=GTEST_FILTER=" + ":".join(sorted(CASES))]))
        for name, owners, extra in phases:
            result = invoke(name, ["test", *options, *extra, *owners], "test", targets=owners)
            check = {"name": name, "result": result, "actual_cases": []}
            record["checks"].append(check)
            if (result["status"] != "PASS" or result["case_counts"]["failed"]
                    or result["case_counts"]["skipped"] or not result["case_counts"]["passed"]):
                raise RuntimeError(f"{name}: expected positive PASS with no missing, skipped or failed cases")
            attempts = result["executed"]
            if len(attempts) != len(owners) or {a["owner"]["label"] for a in attempts} != set(owners):
                raise RuntimeError(f"{name}: unexpected executed owners: {attempts}")
            for attempt in attempts:
                if (attempt["run"] != 1 or attempt["attempt"] != 1 or attempt["cached_locally"]
                        or attempt["execution_info"].get("cachedRemotely", False)
                        or attempt["execution_info"].get("strategy") != "local"):
                    raise RuntimeError(f"{name}: expected uncached local single attempts: {attempt}")
            if name == "netlink-nogo":
                if result["case_counts"]["passed"] != len(ANALYZERS):
                    raise RuntimeError("Expected four native Nogo wrappers with both-architecture dependencies")
                continue
            _, reports, errors = report.retained_reports(report.manifest_path(evidence / ("invocation-" + name) / "events.jsonl"))
            xml_reports = [(attempt, path) for (attempt, kind), path in reports.items() if kind == "test.xml"]
            if errors or len(xml_reports) != len(owners):
                raise RuntimeError(f"{name}: missing or invalid authenticated XML: {errors}")
            for attempt, path in xml_reports:
                cases = list(ET.parse(path).getroot().iter("testcase"))
                names = [case.attrib["name"] for case in cases]
                if name == "netlink-units":
                    required = {
                        UNITS[0]: {"TestDestroyNilStack", "TestRemoveRouteRemovesFirstMatchOnly"},
                        UNITS[1]: {"TestFillRoute", "TestParseForDestination", "TestWriteLinkInfo"},
                        UNITS[2]: {"TestAddressMatchingPrefix"},
                        UNITS[3]: {"TestRoutes", "TestAddProtocolAddress", "TestAddRoute", "TestRemoveRoutes"},
                    }[attempt.owner.label]
                    if not required <= set(names):
                        raise RuntimeError(f"{attempt.owner.label}: missing required unit cases: {required - set(names)}")
                else:
                    names = [case.attrib["classname"] + "." + case.attrib["name"] for case in cases]
                    if set(names) != CASES or len(names) != len(CASES):
                        raise RuntimeError(f"{name}: got {names}; want exact {sorted(CASES)}")
                check["actual_cases"].append({"owner": attempt.owner.label, "names": names, "xml": report.file_record(path)})
        record["status"] = "PASS"
    except Exception:
        record["status"] = "FAIL"
        raise
    finally:
        report.write_json(evidence / "netlink-checks.json", record)
