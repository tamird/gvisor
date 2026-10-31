"""Run the original Go parser on the literal harness output. No product edits."""
import json
from pathlib import Path
import sys

root=Path(sys.argv[1])
is3=sys.argv[2]=="3"
source=Path("test/benchmarks/tools/iperf_test.go")
program=["\nfunc TestActualLinuxIperfOutput(t *testing.T) {"]
for label in (("approved-iperf3",) if is3 else ("jammy-iperf2","ding-iperf2")):
    records=root/label/"observations.json"
    if not records.exists(): continue
    for r in json.loads(records.read_text()):
        if "error" in r: continue
        raw=(root/label/r["case"]/"client.txt").read_text()
        # Warnings to stderr are not JSON; iperf3's stdout is the actual parser input.
        if is3: raw=json.dumps(json.loads(raw),separators=(",",":"))
        expected=r["aggregate_bytes_per_second"] if is3 else r["first_KiBps"]
        name=label+"/"+r["case"]
        program += ["\tt.Run("+json.dumps(name)+", func(t *testing.T) {",
            "\t\ti := Iperf{}",
            "\t\tgot, err := i.bandwidth("+json.dumps(raw)+")",
            "\t\tif err != nil || got != "+repr(expected)+" { t.Fatalf(\"bandwidth = %v (%v)\", got, err) }",
            "\t\tt.Logf(\"actual parser %g\", got)",
            "\t})"]
program+=["}"]
if len(program)<=2: raise RuntimeError("no actual iperf results")
with source.open("a") as f: f.write("\n".join(program)+"\n")
