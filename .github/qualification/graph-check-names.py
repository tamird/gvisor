"""Recover an exact BuildBuddy invocation's test logs before running named cases."""
from pathlib import Path
import hashlib,json,os,re,sys,urllib.request,urllib.parse,urllib.error

out=Path(sys.argv[2])
inv="9508bbf3-6e44-406f-b3e4-60555d01382c"
server="https://app.buildbuddy.io"
expected={"state_test":"TestGraphDecodeMapAndLoadValueDirectStructWithHooks","tests_test":"TestMapLoadHookReceivers"}
def verify():
    for key,case in expected.items():
        data=(out/(key+"-names.txt")).read_text()
        assert re.search(r"(?m)^\s*--- PASS: "+case+r"\s+\(",data) and "FAIL:" not in data and "SKIP:" not in data,case
def varint(blob,i):
    n=0
    for k in range(0,64,7):
        b=blob[i];i+=1;n|=(b&127)<<k
        if b<128:return n,i
    raise ValueError("invalid proto integer")
def parts(blob,field):
    i=0
    while i<len(blob):
        h,i=varint(blob,i);n,t=divmod(h,8)
        if t==0:v,i=varint(blob,i)
        elif t==2:size,i=varint(blob,i);v=blob[i:i+size];i+=size
        elif t in (1,5):size=8 if t==1 else 4;v=blob[i:i+size];i+=size
        else:raise ValueError("invalid proto wire type")
        if n==field:yield v
def request(url,data=None):
    headers={"x-buildbuddy-api-key":os.environ["BUILDBUDDY_API_KEY"]}
    if data:headers["Content-Type"]="application/proto"
    return urllib.request.urlopen(urllib.request.Request(url,headers=headers,data=data),timeout=20).read()
def recover():
    info={}
    for label in ["//pkg/state:state_test","//pkg/state/tests:tests_test"]:
        def field(n,text):
            b=text.encode();assert len(b)<128;return bytes([n*8+2,len(b)])+b
        items=[request(server+"/rpc/BuildBuddyService/GetTarget",field(1,inv)+field(2,label))]
        for f in (1,2,6,10,2):items=[v for item in items for v in parts(item,f)]
        logs=[]
        for item in items:
            if next(parts(item,1),b"")!=b"test.log":continue
            uri=next(parts(item,2)).decode()
            logs.append(request(server+"/file/download?"+urllib.parse.urlencode({"bytestream_url":uri,"invocation_id":inv})))
        assert len(logs)==1,label
        key=label.split(":")[-1];case=expected[key];s=logs[0].decode(errors="replace")
        found=[x[:200] for x in s.splitlines() if re.search(r"(?:=== RUN\s+|--- (?:PASS|FAIL|SKIP): )"+case,x)]
        info[label]={"sha256":hashlib.sha256(logs[0]).hexdigest(),"length":len(logs[0]),"matched_lines":len(found)}
        (out/(key+"-names.txt")).write_text("\n".join(found)+"\n")
    (out/"original-log-status.json").write_text(json.dumps(info,indent=2)+"\n")
if sys.argv[1]=="read":
    try:recover()
    except Exception as e:
        # Do not publish raw replies or authentication material.
        (out/"original-log-error.txt").write_text(type(e).__name__+" "+str(getattr(e,"code",""))+"\n")
        sys.exit(1)
verify()
