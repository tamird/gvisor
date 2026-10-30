import json
from pathlib import Path
import re
import sys
root,out=map(Path,sys.argv[1:])
diags=[]
def put(line):
    if not re.search(r"ERROR|FATAL|WARNING:.*(?:patch|fetch|extension)|[Ee]rror:|[Ff]ailed|[Cc]aused by|Exception|no such |no matching|incompatible|does not|Unknown|Can't|must |not found|invalid|missing|status [1-9]",line):
        return
    if re.search(r"header|token|secret|authoriz|credential|api.?key|[?&]X-",line,re.I):
        return
    line=line[:500]
    if line not in diags and len(diags)<18:diags.append(line)
for evt in sorted((root/"qualification").glob("invocation-*/events.jsonl")):
    for line in evt.read_text(errors="replace").splitlines():
        try:e=json.loads(line)
        except ValueError:continue
        for value in [e.get("progress",{}).get("stderr",""),e.get("aborted",{}).get("description",""),e.get("action",{}).get("failureDetail",{}).get("message","")]:
            for row in value.splitlines():put(row)
for path in [root/"coordinator.log",*sorted((root/"qualification").glob("invocation-*/*.log"))]:
    if path.is_file():
        for line in path.read_text(errors="replace").splitlines():put(line)
out.write_text(json.dumps({"diagnostics":diags},indent=2)+"\n")
print(out.name,diags[:4])
