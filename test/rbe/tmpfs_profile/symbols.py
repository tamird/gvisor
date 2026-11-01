"""Collect symbolized stacks from the controlled benchmark process alone."""
import json
from pathlib import Path
import re
import subprocess
import sys
import time
root=Path(sys.argv[1]);out=root/'records'
def pprof(src,*args):
    run = subprocess.run(["go", "tool", "pprof", *args, str(src)],text=True,capture_output=True)
    if run.returncode:
        raise RuntimeError(run.stderr[-1000:])
    return run.stdout
receipts={}
for mode, leaf in (("EncodeStatefile","saveKernel"),("DecodeStatefile","loadKernel"),("DecodeGC","loadKernel")):
    src=root/'private'/(mode+'.pprof')
    trace = pprof(src,"-traces", "-nodefraction=0")
    assert "Total samples" in trace and len(trace)>100, trace[:300]
    # -traces contains workload-independent Go symbols and sample weights; no host environment or workload bytes.
    (out/(mode+'-stacks.txt')).write_text(trace)
    flat=pprof(src,"-top","-nodecount=120","-nodefraction=0")
    (out/(mode+'-flat-full.txt')).write_text(flat)
    matches=sorted(set(re.findall(r'[^\s]+[.]'+leaf+r'\b',trace)))
    value=re.compile(r'Showing nodes accounting for (?!0(?:[,. ]|$))')
    # The rule-generated Go test import path is allowed to include a test suffix.
    for expr, kind in [(r'[.]'+leaf+r'$',leaf),(r'/pkg/state[.](?:saveGraph|Save)$' if leaf=='saveKernel' else r'/pkg/state[.](?:loadGraph|Load)$','state codec')]:
        focused=pprof(src,"-top","-nodecount=28","-nodefraction=0","-focus="+expr)
        if value.search(focused):
            (out/(mode+'-flat.txt')).write_text(focused)
            (out/(mode+'-cum.txt')).write_text(pprof(src,"-top","-cum","-nodecount=45","-nodefraction=0","-focus="+expr))
            receipts[mode]={"focus":expr,"function":kind,"matched_symbols":matches,"profile_size":src.stat().st_size}
            break
    (out/'symbol-verdict.json').write_text(json.dumps({"modes":receipts,"written_unix_ns":time.time_ns()},indent=2)+'\n')
assert len(receipts)==3, receipts
