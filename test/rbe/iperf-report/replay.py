import json, sys
from pathlib import Path
n,raw=sys.argv[1:]
raw=Path(raw).read_text()
bw=json.loads(raw)['end']['sum_sent']['bits_per_second']/8
s='''
func TestLinuxIperfReport(t *testing.T) {
  report := testing.Benchmark(func(b *testing.B) {
    for n:=0; n<b.N; n++ { (&Iperf{}).Report(b, RAW) }
  })
  rate:=report.Extra["bandwidth.bytes_per_second"]
  t.Logf("go_bytes_per_iteration=%d measured_Bps=%.17g", report.Bytes, rate)
  if report.Bytes!=WANTED || rate!=RATE { t.Fatalf("wrong report: %+v", report) }
}
'''.replace('RAW',json.dumps(raw)).replace('WANTED',n).replace('RATE',repr(bw))
with Path('test/benchmarks/tools/iperf_test.go').open('a') as f: f.write(s)
