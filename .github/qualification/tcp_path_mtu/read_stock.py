"""Rebuild the compact stock comparison from the original ZIP on stdin."""
import io
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from decode import sha
from qualify import validate_row

DIGEST="6101d0ab3aabe539ebceb2bd2d2c8e7687b0ce1901b8c98fa4e8bcfe174bb478"
b=sys.stdin.buffer.read()
assert sha(b)==DIGEST
with zipfile.ZipFile(io.BytesIO(b)) as z:
    old=json.loads(z.read("result.json"))
    log,xml=z.read("test.log"),z.read("test.xml")
assert old["carrier"]=="5e1530fa3fb69e8b32c6561d5e0ae1add9fae8d8" and old["exit"]==0
assert sha(log)==old["test"]["test.log"] and sha(xml)==old["test"]["test.xml"]
assert old["test"]["status"]=="PASSED" and not old["test"]["cached"] and old["test"]["strategy"]=="local"
xt=ET.fromstring(xml)
assert xt.find(".//error") is None and xt.find(".//failure") is None
stock={"artifact_sha256":DIGEST,"url":"https://github.com/tamird/gvisor/actions/runs/42447315664/artifacts/13604865115","source":old["source"],"test":old["test"],"cases":{}}
for i,line in enumerate(re.findall(r"(?m)MTU_RESULT (\{.*\})$",log.decode())):
    r=json.loads(line)
    k=f'{r["Mode"]}/{r["Family"]}/mss_{r["PeerMSS"]}/{r["Phase"]}'
    validate_row(r,i,[])
    assert k not in stock["cases"] and r["Outcome"]!="setup_failure"
    item={v:r[v] for v in ("Outcome","Read","PrimeRead","matched_peer_bytes","stream_bytes","Digest")}
    if k=="none/v4/mss_0/on":
        packets=r["Frames_summary"]["reassembled"]
        assert len(packets)==7 and all(p["id"]==0 and p["tcp"]["checksum_valid"] and p["tcp"]["payload_matches"] for p in packets)
        item["seven_sequential_id_zero_datagrams"]=[p["tcp"]["stream_offset"] for p in packets]
    stock["cases"][k]=item
assert len(stock["cases"])==36
print(json.dumps(stock,indent=2,sort_keys=True))
