"""Collect only the two syscall observations and TCP continuity per system."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys


def observe(d):
    if not (d / "exit").exists():
        return {"verified": False}
    text=(d/"bazel.log").read_text(errors="replace")
    pat=r"ESTAB_MSG opt=([01]) first=(true|false) poll=(\d+) so=(\d+) afterfirst=(\d+) n=(-?\d+) errno=(\d+) flags=(\d+) cbytes=(\d+) level=(\d+) type=(\d+) ee=(\d+) origin=(\d+) itype=(\d+) code=(\d+) again=(\d+) final=(\d+)"
    fields="poll so_error after_first ret errno msg_flags control_bytes cmsg_level cmsg_type extended_errno origin icmp_type icmp_code next_errno final_poll".split()
    rows={f"{m[0]}-{m[1]}":dict(zip(fields,map(int,m[2:]))) for m in re.findall(pat,text)}
    live=sorted({f"{opt}-{first}" for opt,first in re.findall(r"ESTAB_MSG_LIVE opt=([01]) first=(true|false)",text)})
    be=str((d/"bep").read_text()) if (d/"bep").exists() else ""
    events=[json.loads(line) for line in be.splitlines() if line.startswith('{"')]
    inv=[x["started"]["uuid"] for x in events if "started" in x]
    summaries={x["id"]["testSummary"]["label"]:x["testSummary"]["overallStatus"] for x in events if "testSummary" in x}
    rc=int((d/"exit").read_text())
    ok=rc==0 and len(inv)==1 and len(summaries)==1 and list(summaries.values())==["PASSED"] and sorted(rows)==live==["0-false","0-true","1-false","1-true"] and all(r["next_errno"]==11 and r["final_poll"]==0 for r in rows.values()) and all(r["so_error"]==101 for k,r in rows.items() if k.startswith("0-") or k.endswith("-false"))
    return {"verified":ok,"exit":rc,"invocations":inv,"rows":rows,"continued":live,"summaries":summaries,"fixture_sha256":(d/"fixture").read_text().strip().split()[0],"start":(d/"start").read_text().strip(),"finish":(d/"finish").read_text().strip(),"diagnostics":[l[:300] for l in text.splitlines() if (l.startswith(("ERROR:","FAIL:","[ RUN")) or ": error:" in l or ": fatal error:" in l or "unexpected control" in l or "wrong extended" in l) and not any(w in l.lower() for w in ("header","token","key","credential"))][:4]}


if __name__=="__main__":
    root=Path(sys.argv[1])
    rows={arm:observe(root/arm) for arm in ["native","netstack"]}
    ok=all(r["verified"] for r in rows.values()) and len({r.get("fixture_sha256") for r in rows.values()})==1
    (root/"verdict.json").write_text(json.dumps({"verified":ok,"source":os.environ["STOCK"],"network":"IPv6","icmp_code":"network unreachable","arms":rows},indent=2)+"\n")
    if len(sys.argv)==2:
        raise SystemExit(not ok)
