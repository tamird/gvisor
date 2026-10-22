"""Reconcile the Linux selftest runner's outer plan with the selected programs."""
import re


def reconcile(text: str, programs: list[str]) -> dict:
    lines = [line.strip() for line in text.splitlines()]
    headers = [index for index, line in enumerate(lines)
               if line == "TAP version 13" or line.endswith("output: TAP version 13")]
    if len(headers) != 1:
        raise ValueError("Expected one Linux selftest TAP stream")
    lines = lines[headers[0] + 1:]
    plans = [int(line[3:]) for line in lines if re.fullmatch(r"1\.\.[0-9]+", line)]
    if plans != [len(programs)]:
        raise ValueError(f"Selftest plan differs from selected programs: {plans}")
    results = []
    skipped = []
    failed = []
    for line in lines:
        if match := re.fullmatch(r"(ok|not ok) ([0-9]+) selftests: pidfd: (\S+)(.*)", line):
            status, number, program, annotation = match.groups()
            results.append({"number": int(number), "program": program,
                            "status": status, "annotation": annotation.strip()})
        # Linux prefixes each child stream with '# '. Retain child skips and
        # reject their failures even if a program incorrectly exits zero.
        child = re.sub(r"^(?:#\s*)+", "", line)
        if re.match(r"not ok\b", child) or child.startswith("Bail out!"):
            failed.append(line)
        if re.match(r"(?:ok\b|1\.\.0\b)", child) and re.search(r"#\s*SKIP\b", child, re.IGNORECASE):
            skipped.append(line)
    actual = [(result["number"], result["program"]) for result in results]
    if actual != list(enumerate(programs, 1)):
        raise ValueError(f"Executed selftest programs differ: {actual}")
    if failed or any(row["status"] != "ok" or row["annotation"] for row in results):
        raise ValueError(f"A required selftest failed or skipped: {results}; {failed}")
    return {"status": "PASS", "expected_programs": programs, "executed_programs": results,
            "nested_skip_lines": skipped,
            "scope": "All selected programs completed; nested skips remain explicit."}
