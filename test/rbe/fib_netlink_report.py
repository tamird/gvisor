import json, pathlib, re, sys
d=pathlib.Path(sys.argv[1]); names=('MissingDreg', 'MissingResult', 'MissingFlags', 'BothSaddrDaddr', 'BothOifIif', 'InvalidResult', 'DregAboveMax', 'DregWouldTruncate', 'UnknownFlag', 'ValidLastRegister', 'ValidOIF', 'ValidOIFNAME', 'ValidADDRTYPE', 'ValidOIFWithPresent', 'OIFNAMEWithPresent', 'ADDRTYPEWithPresent')
verdict={"cases":{},"errors":[]}
for arm in ("linux","current","original","package"):
    p=d/arm
    row={"source":(p/"source").read_text().strip(),"exit":int((p/"exit").read_text()),"started":(p/"started").read_text().strip(),"ended":(p/"ended").read_text().strip()}
    verdict["cases"][arm]=row
    try:
        events=[json.loads(line) for line in (d.parent/("fib-bep-"+arm)).read_text().splitlines()]
        results=[e["testResult"] for e in events if "testResult" in e]
        assert len(results)==1, ("actual TestRunner count",len(results))
        rr=results[0]; info=rr["executionInfo"]
        row["execution"]={"strategy":info["strategy"],"cached":bool(rr.get("cachedLocally") or info.get("cachedRemotely"))}
        assert not row["execution"]["cached"] and info["strategy"] in ("local","standalone"),row["execution"]
        row["invocation"]=next(e["started"]["uuid"] for e in events if "started" in e)
        log=(p/"test.log").read_text()
        if arm=="package":
            row["retained_test"]=bool(re.search(r"--- PASS: TestFIBResultStorage(?: \(|$)",log,re.MULTILINE))
            assert row["retained_test"] and row["exit"]==0 and "TestFIBAttributes" not in log
            continue
        rows={}
        for name in names:
            # The syscall runner's outer XML may only name its wrapper; use the
            # inner gtest's exact terminal outcomes, including explicit skips.
            pattern=r"\[\s*(OK|FAILED|SKIPPED)\s*\]\s+FibRuleTest/AddRuleWithExprTest\.AddRuleWithExpr/"+name+r"(?:\s|,|$)"
            states={match.group(1) for match in re.finditer(pattern,log,re.MULTILINE)}
            assert len(states)==1, (name,states)
            rows[name]=states.pop()
        row["outcomes"]=rows
        if arm=="original":
            want={"DregAboveMax":"OK","DregWouldTruncate":"FAILED","UnknownFlag":"FAILED","ValidLastRegister":"OK"}
        else:
            want={name:"OK" for name in names}
        if any(rows[name]!=state for name,state in want.items()) or "SKIPPED" in rows.values() or row["exit"]!=(3 if arm=="original" else 0):
            verdict["errors"].append([arm,"unexpected outcome",rows,row["exit"]])
    except (OSError,AssertionError,KeyError,ValueError,StopIteration) as exc:
        verdict["errors"].append([arm,repr(exc)])
(d/"verdict.json").write_text(json.dumps(verdict,indent=2)+"\n")
print(json.dumps(verdict,indent=2))
assert not verdict["errors"],verdict["errors"]
