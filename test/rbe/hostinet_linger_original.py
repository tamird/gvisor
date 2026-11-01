"""Recover original timestamped BuildBuddy results without repeating tests."""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json
import os
import re
import time
import xml.etree.ElementTree as ET

SOURCE = "c53942792678277f0195068aa41ad17620dd9dd1"
START = "2026-11-01T01:49:21Z"
SYSCALLS_DONE = "2026-11-01T01:53:13Z"
END = "2026-11-01T04:40:00Z"
PREFIX = "AllInetTests/TcpSocketTest."
CASES = {f"{PREFIX}{n}/{i}" for n in ("NoLingerOnProcessExit", "AbortiveLingerOnExit") for i in range(2)}
HOST = "https://app.buildbuddy.io"
TARGETS = {x:"//test/syscalls:tcp_socket_test_"+t for x,t in [("linux","native"),("netstack","runsc_systrap"),("hostinet","runsc_systrap_hostnet")]}
TARGETS["analyzer"] = "//pkg/sentry/socket/hostinet:hostinet_nogo"

def call(method, req, *, rpc=False, data=False):
    url = HOST + ("/rpc/BuildBuddyService/" if rpc else "/api/v1/") + method
    request = Request(url, data=json.dumps(req).encode(), headers={"Content-Type":"application/json","x-buildbuddy-api-key":os.environ["BUILDBUDDY_API_KEY"]})
    for n in range(3):
        try:
            with urlopen(request, timeout=32) as r:
                body = r.read(20*1024*1024+1)
            if len(body) > 20*1024*1024:
                raise ValueError("response too large")
            return body if data else json.loads(body)
        except HTTPError as e:
            if n==2 or e.code not in (429, 502, 503, 504):
                raise RuntimeError(f"{method} HTTP {e.code}") from None
            time.sleep(2)

def field(o,k):
    if k in o:
        return o[k]
    camel = re.sub(r"_([a-z])", lambda m:m[1].upper(), k)
    return o.get(camel)

def get_id(row):
    return field(row,"invocation_id") or field(row.get("id",{}),"invocation_id")

def stamp(usecs):
    return datetime.fromtimestamp(int(usecs)/1e6, timezone.utc).isoformat() if usecs else None

def xml_verdict(data):
    cases = {}
    for c in ET.fromstring(data).iter("testcase"):
        name=c.get("classname","")+"."+c.get("name","")
        if name in cases:
            raise ValueError("duplicate case")
        cases[name] = "SKIP" if c.find("skipped") is not None or c.get("status")=="notrun" else "FAIL" if c.find("failure") is not None or c.find("error") is not None else "OK"
    if not cases:
        raise ValueError("empty GTest XML")
    return cases

def observe(lane, target):
    begin = "2026-11-01T01:53:11Z" if lane == "analyzer" else START
    end = END if lane == "analyzer" else SYSCALLS_DONE
    try:
        data=call("SearchInvocation",{"query":{"pattern":target,"updated_after":begin,"updated_before":end},"count":32},rpc=True)
        matches=data.get("invocation",[])
    except RuntimeError as e:
        # The public API can still identify this SHA on installations that
        # reserve the Web query for interactive accounts.
        fetched=call("GetInvocation",{"selector":{"commit_sha":SOURCE}}).get("invocation",[])
        matches=[inv for inv in fetched if target in inv.get("pattern","").split() and datetime.fromisoformat(begin) <= datetime.fromtimestamp(int(field(inv,"updated_at_usec"))/1e6,timezone.utc) < datetime.fromisoformat(end)]
        if not matches:
            return {"verified":False,"lookup":str(e),"sha_lookup_matches":0}
    if len(matches)!=1:
        return {"candidate_count":len(matches),"verified":False}
    iid=get_id(matches[0])
    if not iid or not re.fullmatch(r"[a-f0-9-]{36}",iid):
        raise ValueError("missing invocation identity")
    inv=call("GetInvocation",{"selector":{"invocation_id":iid},"include_metadata":True})["invocation"][0]
    ws={(v["key"]):v.get("value","") for v in (field(inv,"workspace_status") or [])}
    rev=ws.get("STABLE_VERSION","")
    stamp_ok = SOURCE[:12] in rev or field(inv,"commit_sha")==SOURCE
    exists = {"uuid":iid,"source_stamp_matches":stamp_ok,"created":stamp(field(inv,"created_at_usec")),"updated":stamp(field(inv,"updated_at_usec")),"status":field(inv,"invocation_status"),"success":inv.get("success",False),"verified":False}
    if not stamp_ok:
        return exists
    if lane=="analyzer":
        tg=call("GetTarget",{"selector":{"invocation_id":iid,"label":target}}).get("target",[])
        exists["target_count"]=len(tg)
        exists["target_status"]=[x.get("status") for x in tg]
        exists["verified"] = exists["success"] and len(tg)==1 and tg[0].get("status") in (5,"PASSED")
        if not exists["verified"]:
            classes=set(); labels=set(); page=""
            for _ in range(32):
                d=call("GetLog", {"selector":{"invocation_id":iid}, "page_token":page})
                for line in d.get("log",{}).get("contents","").splitlines():
                    for term in ("resource exhausted","resourceexhausted","out of memory","deadline exceeded","killed","failed:","timed out","cancelled"):
                        if term in line.lower():
                            classes.add(term)
                            labels.update(re.findall(r"(?<!:)[/]{2}(?:pkg|runsc|tools)/[A-Za-z0-9_/.-]+:[A-Za-z0-9_.-]+",line))
                page=field(d,"next_page_token")
                if not page:
                    break
            exists["diagnostic_classes"] = sorted(classes)
            exists["diagnostic_targets"] = sorted(labels)[:12]
        return exists
    acts=call("GetAction",{"selector":{"invocation_id":iid,"target_label":target}}).get("action",[])
    uris={f.get("uri") for a in acts for f in a.get("file",[]) if f.get("name","").split("/")[-1]=="test.xml" and f.get("uri")}
    if len(uris)==1:
        uri=next(iter(uris))
        ident=re.search(r"/(?:blobs|compressed-blobs/zstd)/([0-9a-f]{64})/(\d+)$",uri)
        if ident:
            try:
                xml=call("GetFile",{"uri":uri},data=True)
                if sha256(xml).hexdigest()!=ident[1] or len(xml)!=int(ident[2]):
                    raise ValueError("GTest digest mismatch")
                exists["cases"]=xml_verdict(xml)
                exists["receipt"]="xml:"+ident[1]
            except RuntimeError:
                pass
    if "cases" not in exists:
        outputs={};page="";prev="";observed=set()
        for _ in range(32):
            log=call("GetLog",{"selector":{"invocation_id":iid},"page_token":page})
            chunk=prev+log.get("log",{}).get("contents","")
            for label,case in re.findall(r"\[\s*(RUN|OK|FAILED|SKIPPED)\s*\]\s+(AllInetTests/TcpSocketTest\.\w+/\d+)",chunk):
                if label=="RUN":
                    observed.add(case)
                else:
                    outputs[case]=label
            prev=chunk[-300:]
            page=field(log,"next_page_token")
            if not page:
                break
        exists.update(cases=outputs,started=sorted(observed),receipt="GTest console")
    cases=exists["cases"]
    exists["verified"]=exists["success"] and set(cases)==CASES and all(x=="OK" for x in cases.values()) and set(exists.get("started",CASES))==CASES
    return exists

def main():
    result={"action_run":41262656868,"source":SOURCE}
    for key,target in TARGETS.items():
        try:
            result[key]=observe(key,target)
        except (ValueError, RuntimeError, KeyError, OSError) as e:
            result[key]={"verified":False,"problem":str(e)[:100]}
    result["verified_at"]=datetime.now(timezone.utc).isoformat()
    result["verified"]=all(result[k]["verified"] for k in TARGETS)
    Path("original-verdict.json").write_text(json.dumps(result,indent=2)+"\n")

if __name__=="__main__":
    main()
