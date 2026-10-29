"""Evidence for the stock and corrected highest-sequence TCP probe controls."""
import re
from xml.etree import ElementTree

from matched_test_result import check_test


def observe(suite, stock, wrapper, lanes, test_status, xml, log, aborted):
    old = suite == "TestTailProbeLastPacket"
    if not old and suite != "TestGSOTailLossProbe":
        raise ValueError("unknown tail probe fixture")
    variants = ("ordinary", "software", "softwareShortTail") if old else ("full", "short")
    expected = {suite, *(f"{suite}/{v}" for v in variants)}
    failed = expected - {f"{suite}/ordinary"} if stock else set()
    cases = ElementTree.fromstring(xml).findall(".//testcase")
    native = check_test(wrapper, lanes, test_status, cases, expected, failed, aborted)
    result = {"wrapper_exit": wrapper, "native_exit": native, "test_status": test_status,
              "cases": [{"name": c.attrib["name"], "passed": c.find("failure") is None} for c in cases]}
    if not stock:
        return result
    # Reject additional assertions in either leaf, even if a known failure is also present.
    diagnostics = {}
    for case in cases:
        name = case.attrib["name"]
        if name not in failed or name == suite:
            continue
        failure = case.find("failure")
        message = " ".join(failure.attrib.values()) + " " + "".join(failure.itertext())
        lines = re.findall(r"[\w./-]+[.]go:\d+: ([^\n\r<>\x0e\x0f]+)", message)
        if not lines or any(line.strip() not in log for line in lines):
            raise ValueError(f"unmatched original log/XML diagnostic in {name}")
        if old:
            if len(set(lines)) != 1:
                raise ValueError(f"unexpected additional failure in {name}")
            match = re.fullmatch(r"packet starts at (\d+), want (\d+) \(offsets 10, 30\)", lines[0].strip())
            if not match or (int(match[2]) - int(match[1])) % (1 << 32) != 20:
                raise ValueError(f"unexpected packet offsets in {name}")
            diagnostics[name] = [10, 30]
        else:
            seq = {line.strip() for line in lines if line.startswith("Bad sequence number,")}
            if len(seq) != 1:
                raise ValueError(f"missing or ambiguous tail sequence in {name}")
            match = re.fullmatch(r"Bad sequence number, got = (\d+), want = (\d+)", seq.pop())
            if not match or (int(match[2]) - int(match[1])) % (1 << 32) != 28:
                raise ValueError(f"unexpected tail sequence in {name}")
            required = {match[0]}
            if name.endswith("/short"):
                required.add("Bad payload length, got = 34, want = 27")
            if {line.strip() for line in lines} != required:
                raise ValueError(f"unexpected additional failure in {name}")
            diagnostics[name] = {"offset_difference": 28}
    result["stock_diagnostics"] = diagnostics
    return result
