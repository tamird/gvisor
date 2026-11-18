"""Describe actual CLI reports without turning request errors into valid metrics."""

import csv
import io
import json
import math
from pathlib import Path
import re
import sys


def parse_hey(output, count, expected_status):
    statuses = {}
    errors = []
    section = None
    for line in output.splitlines():
        stripped = line.strip()
        if stripped == "Status code distribution:":
            section = "status"
            continue
        if stripped == "Error distribution:":
            section = "error"
            continue
        if section == "status":
            match = re.fullmatch(r"\[(\d+)\]\s+(\d+)\s+responses", stripped)
            if stripped and not match:
                raise ValueError(f"Unparsed Hey status-accounting line: {stripped}")
            if match:
                code, amount = map(int, match.groups())
                if code in statuses:
                    raise ValueError("Duplicate status code")
                statuses[code] = amount
        elif section == "error":
            match = re.fullmatch(r"\[(\d+)\]\s+(.+)", stripped)
            if stripped and not match:
                raise ValueError(f"Unparsed Hey error-accounting line: {stripped}")
            if match:
                errors.append({"count": int(match[1]), "message": match[2]})
    total = re.search(r"\bTotal:\s*([0-9.]+)\s+secs", output)
    rate = re.search(r"Requests/sec:\s*(\S+)", output)
    if not total or not rate or not (statuses or errors):
        raise ValueError("Missing Hey summary/status/error accounting")
    seconds = float(total[1])
    requests_per_second = float(rate[1])
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Invalid Hey elapsed time")
    finite_rate = math.isfinite(requests_per_second) and requests_per_second > 0
    error_count = sum(item["count"] for item in errors)
    data = re.search(r"Total data:\s*(\d+)\s+bytes", output)
    return {"clientReportedDataBytes": int(data[1]) if data else None, "requested": count, "expectedStatus": expected_status, "statusCounts": statuses, "errors": errors,
            "errorCount": error_count, "seconds": seconds, "requestsPerSecond": requests_per_second if finite_rate else None,
            "allExpectedResponses": statuses == {expected_status: count} and error_count == 0 and finite_rate,
            "limit": "Reported client status/error counts; not physical wire traffic."}


def parse_redis(output, operation):
    rows = list(csv.reader(io.StringIO(output)))
    header_index = next((index for index, row in enumerate(rows) if row and row[0].strip().lower() == "test" and "rps" in row), None)
    if header_index is None:
        raise ValueError("Missing Redis CSV header")
    header = rows[header_index]
    selected = [row for row in rows[header_index + 1:] if row and (row[0].strip() == operation or row[0].strip().startswith(operation + " "))]
    if len(selected) != 1 or len(selected[0]) != len(header):
        raise ValueError("Missing or ambiguous Redis operation row")
    values = {name: float(value) for name, value in zip(header[1:], selected[0][1:])}
    setup = [row for row in rows[header_index + 1:] if row and row[0] == "LPUSH (needed to benchmark LRANGE)"]
    if setup and (not operation.startswith("LRANGE_") or len(setup) != 1 or rows.index(setup[0]) > rows.index(selected[0])):
        raise ValueError("Unexpected Redis LRANGE setup row")
    setup_metrics = []
    for row in setup:
        if len(row) != len(header):
            raise ValueError("Malformed Redis LRANGE setup row")
        metrics = {name: float(value) for name, value in zip(header[1:], row[1:])}
        setup_metrics.append(metrics)
    unexpected = [row for index, row in enumerate(rows) if row and index != header_index and row not in selected and row not in setup]
    def valid_metrics(metrics):
        return all(math.isfinite(value) and value >= 0 for value in metrics.values()) and metrics["rps"] > 0
    valid = not unexpected and valid_metrics(values) and all(valid_metrics(metrics) for metrics in setup_metrics)
    return {"unexpectedRows": unexpected, "operation": operation, "metrics": {name: value if math.isfinite(value) else None for name, value in values.items()},
            "setupMetrics": [{name: value if math.isfinite(value) else None for name, value in metrics.items()} for metrics in setup_metrics],
            "finitePositiveRate": valid, "limit": "CLI CSV does not report the actual retry count or completed-operation count; LRANGE's separately reported LPUSH setup is retained and validated."}



def inspect_directory(directory, expected, category):
    records = []
    final = {}
    seen = set()
    for path in sorted(directory.glob("*.json")):
        item = json.loads(path.read_text())
        name = item["benchmark"]
        key = name if name in expected else name + "-4"
        if key not in expected or item["n"] not in (1, expected[key]):
            raise ValueError("Unexpected raw benchmark name/count")
        pair = (key, item["n"])
        if pair in seen:
            raise ValueError("Duplicate raw benchmark/count report")
        seen.add(pair)
        if item["tool"] == "hey" and category != "redis":
            status = 404 if "/filesize.notfound/" in key else 200
            report = parse_hey(item["output"], item["n"], status)
            valid = report["allExpectedResponses"]
        elif item["tool"] == "redis" and category == "redis":
            operation = key.removesuffix("-4").rsplit("/operation.", 1)[1]
            report = parse_redis(item["output"], operation)
            valid = report["finitePositiveRate"]
        else:
            raise ValueError("Unexpected raw tool")
        record = {"case": key, "n": item["n"], "file": path.name, "valid": valid, "report": report}
        records.append(record)
        if item["n"] == expected[key]:
            if key in final:
                raise ValueError("Duplicate final raw report")
            final[key] = record
    missing = sorted(set(expected) - set(final))
    missing_calibration = sorted(key for key in expected if (key, 1) not in seen)
    return {"records": records, "missingFinalReports": missing, "missingCalibrationReports": missing_calibration,
            "allFinalValid": not missing and all(record["valid"] for record in final.values()),
            "allRecordsValid": bool(records) and not missing_calibration and all(record["valid"] for record in records)}


if __name__ == "__main__":
    expected = json.load(sys.stdin)
    print(json.dumps(inspect_directory(Path(sys.argv[1]), expected, sys.argv[2])))
