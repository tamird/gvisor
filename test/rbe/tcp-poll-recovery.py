"""Read only selected outcomes, named Go cases and bounded remote CAS receipts."""
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
from urllib.request import Request, urlopen

out = Path(os.environ["RUNNER_TEMP"]) / "tcp-evidence"
source = os.environ["PRODUCT"]
iid = "66cd2f03-1dd6-42a4-80c3-45fa792e42ee"
owners = ["//pkg/tcpip/transport/tcp:tcp_test"] + ["//pkg/tcpip/transport/tcp/test/e2e:" + t for t in "tcp_test dual_stack_test forwarder_test rcv_test sack_scoreboard_test tcp_timestamp_test tcp_rack_test tcp_acktiming_test tcp_sack_test tcp_noracedetector_test".split()]
lint = ["//pkg/tcpip/transport/tcp:tcp_nogo", "//pkg/tcpip/transport/tcp/test/e2e:tcp_test_nogo"]


def go_counts(text):
    ended = re.findall(r"(?m)^\s*--- (PASS|FAIL|SKIP): (\S+)", text)
    return {"outcomes": dict(Counter(k for k, _ in ended)),
            "skips": sorted(n for k, n in ended if k == "SKIP"),
            "failures": sorted(n for k, n in ended if k == "FAIL")}


def rpc(method, req, raw=False):
    url = "https://app.buildbuddy.io/api/v1/" + method
    request = Request(url, data=json.dumps(req).encode(), headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ['BUILDBUDDY_API_KEY']})
    with urlopen(request, timeout=24) as resp:
        data = resp.read(16*1024*1024+1)
    if len(data)>16*1024*1024:
        raise ValueError('oversized response')
    return data if raw else json.loads(data)


def recover_original():
    rows = {}
    for label in owners:
        try:
            acts = rpc("GetAction", {"selector": {"invocation_id": iid, "target_label": label}}).get("action",[])
            uris = {f['uri'] for a in acts for f in a.get('file', []) if f.get('name','').split('/')[-1]=='test.log' and f.get('uri')}
            if len(uris) != 1:
                rows[label] = {"status":"no unique original test log", "distinct":len(uris)}
                continue
            uri, = uris
            ident = re.search(r'/(?:blobs|compressed-blobs/zstd)/([0-9a-f]{64})/(\d+)$', uri)
            if not ident: raise ValueError('unsupported digest')
            data=rpc('GetFile',{'uri':uri},raw=True)
            if int(ident[2]) != len(data) or ident[1] != sha256(data).hexdigest():
                raise ValueError('CAS digest mismatch')
            text=data.decode(errors='replace')
            rows[label]={'sha256':ident[1], 'bytes':len(data), 'go_pass_footer': bool(re.search(r'(?m)^PASS\s*$',text)), **go_counts(text)}
        except Exception as e:
            rows[label]={'error_type':type(e).__name__}
    (out/'original-go-logs.json').write_text(json.dumps({'invocation':iid, 'targets':rows},indent=2)+'\n')


def first_errors():
    ids = {
        "arm-owners": "64a7ca56-2337-4619-873f-622b8aedcabf",
        "arm-stdlib": "e6b69c62-6f00-4e61-911b-28607f80e7bb",
        "msg-native": "9ffb29d8-726a-4041-bc65-4560cd68b9a3",
        "msg-netstack": "ad50d4ea-cb9f-4192-8566-d750a2c94ef3",
    }
    errors = {}
    for name, uuid in ids.items():
        if not name.startswith("arm-"): continue
        after = 0
        request = {"selector": {"invocation_id": uuid}, "page_token": "0000"}
        kept = []
        lines = 0
        size = 0
        for _ in range(15):
            part = rpc("GetLog", request)
            log = part.get("log", {}).get("contents", "")
            size += len(log)
            for line in log.splitlines():
                # Never publish command lines, env, headers, URLs or tokens.
                clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).strip()
                if "error executing GoStandardLibraryAnalysis" in clean:
                    after = 9
                if any(x in clean.lower() for x in ["bearer", "api-key", "credential", "header", "token=", "https:", "command line", "command (from target", "--", "ghp_"]):
                    continue
                near = after > 0 and 0 < len(clean) < 600 and not clean.startswith(("INFO:", "ERROR:", "Use ", "Target ", "//", "bazel", "(", "[")) and not re.search(r'(?:[A-Za-z0-9_+=/-]{50,})', clean)
                if after > 0: after -= 1
                if near or re.search(r"(: error:|: fatal error:|(?:error|exception|fatal|panic):|no such file|exec format|failed to (?:load|parse|analyze|run)|cannot (?:read|open|find)|requires cgo|build cache is|not in std|not defined)", clean, re.I):
                    clipped = clean[:420]
                    if clipped not in kept and len(kept) < 12:
                        kept.append(clipped)
                lines += 1
            page = part.get("nextPageToken",part.get("next_page_token"))
            if not page:
                break
            request["page_token"] = page
        errors[name] = {"invocation": uuid, "checked_lines": lines, "bytes": size, "diagnostics": kept}
    (out/"prior-action-errors.json").write_text(json.dumps(errors,indent=2)+"\n")
    for key, val in errors.items():
        print(key, val["invocation"], *val["diagnostics"], sep="\n")


def native_test_scope(name):
    if name not in ("owners","analysis"):
        raise ValueError("an explicit native TCP suite is required")
    if os.uname().machine != os.environ["TEST_ARCH"]:
        raise ValueError("TestRunner must run on the selected native architecture")
    return owners if name == "owners" else lint


def inspect(name, labels):
    d=out/name
    if not (d/'exit').is_file(): return {'verified':False,'missing':labels}
    actual={}
    inv=[]
    for line in (d/'bep').open() if (d/'bep').is_file() else []:
        try: e=json.loads(line)
        except ValueError: continue
        if 'started' in e: inv.append(e['started']['uuid'])
        if 'testSummary' in e: actual[e['id']['testSummary']['label']]=e['testSummary']['overallStatus']
    rows={}
    for label in labels:
        log=Path('bazel-testlogs')/label.removeprefix('//').replace(':','/')/'test.log'
        if not log.exists(): continue
        text=log.read_text(errors='replace')
        rows[label]={'digest':sha256(log.read_bytes()).hexdigest(),**go_counts(text)}
    rc=int((d/'exit').read_text())
    ok=bool(labels) and rc==0 and set(actual)==set(labels) and all(v=='PASSED' for v in actual.values())
    if name=='owners': ok &= len(rows)==len(labels) and all(r['outcomes'].get('PASS',0)>0 and not r['failures'] for r in rows.values())
    detail=[s[:300] for s in (d/'log').read_text(errors='replace').splitlines() if (s.startswith(('ERROR:','FAIL:','/bin/bash:')) or ': error:' in s or ': fatal error:' in s or 'exec format' in s.lower() or 'cannot execute' in s.lower()) and not any(x in s.lower() for x in ['token','header','credential','api-key'])][:6]
    return {'verified':ok,'source':source,'host_arch':os.uname().machine,'exit':rc,'start':(d/'start').read_text().strip(),'end':(d/'end').read_text().strip(),'invocations':inv,'target_status':actual,'missing':sorted(set(labels)-actual.keys()), 'individual_go_results':rows, 'first_diagnostics':detail}


if sys.argv[1]=='original':
    recover_original()
elif sys.argv[1]=='prior-errors':
    first_errors()
elif sys.argv[1]=='labels':
    print('\n'.join(native_test_scope(sys.argv[2])))
elif sys.argv[1]=='inspect':
    n=sys.argv[2];r=inspect(n,owners if n=='owners' else lint)
    (out/(n+'.json')).write_text(json.dumps(r,indent=2)+'\n')
elif sys.argv[1]=='finish':
    ok=all(json.loads((out/(n+'.json')).read_text())['verified'] for n in ['owners','analysis'])
    raise SystemExit(not ok)
