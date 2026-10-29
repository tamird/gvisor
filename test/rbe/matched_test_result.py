"""Check original single-test qualification outcomes without confusing the two exits."""
import json
from pathlib import Path
from xml.etree.ElementTree import Element


def check_test(wrapper_exit: int, lanes: list[dict], test_status: str, cases: list[Element],
               expected: set[str], failed: set[str], aborted: list[str]) -> int:
    """Return the recorded Bazel exit only after the exact XML outcome agrees."""
    if len(lanes) != 1 or lanes[0].get("lanes") != ["test"]:
        raise ValueError("missing or ambiguous test lane")
    native = lanes[0]["native_exit"]
    if aborted or (wrapper_exit, native, test_status) != (
        (1, 3, "FAILED") if failed else (0, 0, "PASSED")
    ):
        raise ValueError("wrapper, Bazel lane and TestResult did not complete as expected")
    names = [case.attrib["name"] for case in cases]
    if len(names) != len(expected) or set(names) != expected:
        raise ValueError("missing, duplicate or unexpected XML testcase")
    if any(case.find(tag) is not None for case in cases for tag in ("skipped", "error")):
        raise ValueError("XML contains a skipped or errored testcase")
    if {case.attrib["name"] for case in cases if case.find("failure") is not None} != failed:
        raise ValueError("wrong set of failed XML testcases")
    return native


def read_lanes(out: Path) -> list[dict]:
    return [json.loads(line) for line in (Path(out) / "lanes.jsonl").read_text().splitlines()]
