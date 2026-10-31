#!/usr/bin/env python3
"""Preserve original Bazel exits, Go assertions, measured samples and work counts."""
import json
import os
from pathlib import Path
import re
from statistics import median
import sys
from datetime import datetime, timezone

from qualification_report import xml_cases

root = Path(sys.argv[1])
arms = {}
errors = []
pattern = re.compile(
    r"WINDOW-SAVE n=(\d+) order=(narrow|wide) sample=(\d+) save_ns=(\d+) bytes=(\d+) links=(\d+) buckets=(\d+)"
)
for arm, source in (("base", os.environ["SOURCE"]), ("counters", os.environ["COUNTERS"])):
    d = root / arm
    def read(name):
        f = d / name
        return f.read_text().strip() if f.exists() else ""
    log = read("test.log")
    rows = {}
    for n, order, sample, ns, size, links, buckets in pattern.findall(log):
        key = (int(n), order, int(sample))
        if key in rows:
            errors.append(f"{arm}: duplicate sample {key}")
        rows[key] = (int(ns), int(size), int(links), int(buckets))
    expected = {(n, order, i) for n in (128,256,512,1024,2048) for order in ("narrow","wide") for i in range(6)}
    missing = sorted(expected - rows.keys())
    if rows.keys() != expected:
        errors.append(f"{arm}: missing/extra samples {missing}/{sorted(rows.keys()-expected)}")
    if read("exit") != "0" or read("source") != source or read("source-after") != source:
        errors.append(f"{arm}: source or original Bazel exit")
    if not (d/"source-before-status").is_file() or not (d/"source-after-status").is_file() or read("source-before-status") or read("source-after-status"):
        errors.append(f"{arm}: status")
    if not read("start") or not read("end") or not read("fixture-sha256") or not read("product-sha256"):
        errors.append(f"{arm}: missing receipt")
    markers = re.findall(r"--- (PASS|FAIL|SKIP): (\S+)", log)
    if markers != [("PASS", "TestGraphGrowingWindows")]:
        errors.append(f"{arm}: Go cases {markers}")
    try:
        xml = xml_cases(d/"test.xml")
    except (OSError, ValueError, SyntaxError) as err:
        xml = {"error":str(err)}
    if not xml.get("passed") or xml.get("failed") != 0 or xml.get("skipped") != 0:
        errors.append(f"{arm}: XML {xml}")
    summaries = {}
    for n in (128,256,512,1024,2048):
        summaries[n] = {}
        for order in ("narrow","wide"):
            samples = [rows[(n,order,i)] for i in range(1,6) if (n,order,i) in rows]
            if not samples:
                continue
            ns, sizes, links, buckets = zip(*samples)
            summaries[n][order] = dict(median_save_ns=median(ns), image_bytes=sorted(set(sizes)),
                link_steps=sorted(set(links)), bucket_range=[min(buckets),max(buckets)])
        if all(x in summaries[n] for x in ("narrow","wide")) and summaries[n]["narrow"]["image_bytes"] != summaries[n]["wide"]["image_bytes"]:
            errors.append(f"{arm}: unequal output lengths n={n}")
    arms[arm] = dict(source=source, original_exit=read("exit"), start=read("start"), end=read("end"),
        fixture_hash=read("fixture-sha256"), product_hash=read("product-sha256"), xml=xml, missing=missing,
        arguments=read("argv").splitlines(), warm_samples=summaries)
if arms["base"]["fixture_hash"] != arms["counters"]["fixture_hash"]:
    errors.append("different fixtures")
result = dict(ok=not errors, errors=errors, arms=arms, verified=datetime.now(timezone.utc).isoformat())
(root/"verdict.json").write_text(json.dumps(result,indent=2)+"\n")
print(json.dumps(result))
sys.exit(bool(errors))
