"""Compare actual shipped archives and flavor inputs on the corrected client."""

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import uuid
from xml.etree import ElementTree

from archive_alias import observe as observe_alias
import registry_shipping as q
from cold_repository import headlines

SOURCE = "0a0fa1efbacbe9f18b377a954f3f249001c32e21"
BSDTAR = "ae24dbc3ecf6ad628c2fdd205a52347fd4446589e750469766669e25cb80e22b"
KINDS = ("bz2","zstd")
WRITERS = {"//debian:gvisor-release-tar-" + kind + "_archive":kind for kind in KINDS}
DOWNLOAD = "//debian:gvisor-release-tar-"
FLAVORS = ("","-coverage","-race","-race-coverage")
ROOTS = ["//:release", *(DOWNLOAD + kind for kind in KINDS), *(DOWNLOAD + kind + ".tar." + kind for kind in KINDS)]


def flags(arch):
    return [*q.flags(arch,"baseline"), "--modify_execution_info=^Tar$=+no-remote-cache"]


def graph(arch):
    opts = [*q.command("archive",False), "aquery", *flags(arch), "--remote_executor=", "--remote_cache=",
            "--bes_backend=", "--include_artifacts=false","--output=jsonproto",
            'mnemonic("^(Tar|PackageTar)$", deps(set(' + ' '.join(ROOTS) + ')))']
    p = subprocess.run(opts,capture_output=True)
    doc = {"source":SOURCE,"architecture":arch,"embed_label":q.TRIAL,"returncode":p.returncode,"matched":False}
    name = "tar-graph-" + arch
    if p.returncode:
        doc["diagnostic"] = headlines(p.stderr)
        q.save(name,doc)
        return False
    obj = json.loads(p.stdout)
    labels = {str(t["id"]):t["label"].lstrip("@") for t in obj["targets"]}
    cfg = {str(c["id"]):c for c in obj["configuration"]}
    rows = {}
    for a in obj["actions"]:
        owner = labels[str(a["targetId"])]
        conf = cfg[str(a["configurationId"])]
        args = a.get("arguments",[])
        rows[owner] = {"mnemonic":a["mnemonic"],"cpu_config":conf["mnemonic"],"config":conf["checksum"],
                       "platform":a["executionPlatform"].lstrip("@"),"args":args,"actionKey":a["actionKey"]}
    doc["actions"] = rows
    doc["matched"] = (rows.keys() == WRITERS.keys() and all(r["mnemonic"] == "Tar" and r["platform"] == q.WORKER
                      and q.TARGETS[arch][1] in r["cpu_config"] and bool(r["args"]) for r in rows.values()))
    q.save(name,doc)
    return doc["matched"]


def canonical(label):
    name = label.lstrip("@")
    if ":" not in name:
        name += ":" + name.rsplit("/",1)[-1]
    return name


def query_rules(mode):
    result = subprocess.run([*q.command(mode,False),"query","--color=no","--curses=no",
                             "--repository_cache=/downloads","--output=xml","//debian:all"],capture_output=True)
    if result.returncode:
        q.save("variant-query-"+mode,{"bazel_exit":result.returncode,"diagnostic":headlines(result.stderr)})
        return {}
    return {r.get("name"):r for r in ElementTree.fromstring(result.stdout).iter("rule")}


def list_attr(rule, key):
    val = rule.find(f"*[@name='{key}']")
    return [] if val is None else [e.get("value") for e in val.iter("label")]


def pairs(rule,key):
    val = rule.find(f"*[@name='{key}']")
    if val is None:
        return {}
    return {p[0].get("value"):p[1].get("value") for p in val.iter("pair")}


def variants():
    old, new = query_rules("baseline"), query_rules("archive")
    observed = {}
    q.save("flavors",{"source":SOURCE,"rows":observed,"all":False})
    for suffix in FLAVORS:
        for kind in KINDS:
            owner = "//debian:gvisor-release" + suffix + "-tar-" + kind
            if owner not in old and owner not in new and "coverage" in suffix:
                observed[owner] = {"not_declared_in_either_source":True}
                continue
            previous = old[owner]
            expected = {canonical(t):canonical(t).split(":")[-1] for t in list_attr(previous,"srcs")}
            expected.update({canonical(t):d for t,d in pairs(previous,"files").items()})
            for inner in list_attr(previous,"deps"):
                expected.update({canonical(t):"gvisor-bin/"+d for t,d in pairs(old[inner],"files").items()})
            writer = new[owner+"_archive"]
            spec = writer.find("*[@name='mtree']").get("value")
            manifest = pairs(new[spec],"substitutions").get("{content}","")
            selected = {canonical(t):dest for dest,t in re.findall(r"(?m)^[.]/(\S+) type=file content=\$\(location ([^\s)]+)\)$",manifest)}
            sources = sorted(canonical(x) for x in list_attr(writer,"srcs"))
            observed[owner] = {"original":expected,"candidate":selected,"inputs":sources,
                               "matches":expected == selected and set(expected.values()) == q.MEMBERS and sorted(selected) == sources}
            q.save("flavors",{"source":SOURCE,"rows":observed,"all":False})
    ok = len(observed) == 8 and all(r.get("matches") or r.get("not_declared_in_either_source") for r in observed.values())
    q.save("flavors",{"source":SOURCE,"rows":observed,"all":ok})
    return ok


def build(arch):
    key = "archive-"+arch
    iid = str(uuid.uuid4())
    doc = {"source":SOURCE,"embed_label":q.TRIAL,"architecture":arch,"invocation":iid}
    q.save(key,doc)
    args = [*q.command("archive",True),"build",*flags(arch),"--spawn_strategy=remote","--noremote_local_fallback",
            "--remote_accept_cached","--remote_download_outputs=toplevel","--remote_download_regex=.*/tools/zstd/.*compressor$",
            "--nobuild_event_json_file_path_conversion","--build_event_publish_all_actions","--build_event_json_file=/data/"+key+".bep",
            "--execution_log_compact_file=/data/"+key+".compact","--invocation_id="+iid,*ROOTS]
    with (q.W/"data"/(key+".log")).open("wb") as f:
        doc["bazel_exit"] = subprocess.run(args,stdout=f,stderr=f).returncode
    q.save(key,doc)
    print(key,"Bazel",doc["bazel_exit"],flush=True)


def stream_hash(stream):
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def headers(tar):
    rows = {}
    duplicate = []
    for member in tar:
        name = member.name
        if name in rows:
            duplicate.append(name)
        if name not in q.MEMBERS | {"gvisor-bin","./gvisor-bin","."} and not (name.startswith("./") and name[2:] in q.MEMBERS):
            raise ValueError("unexpected archive member")
        r = {"type":member.type.decode("ascii"),"uid":member.uid,"gid":member.gid,"uname":member.uname,"gname":member.gname,
             "mode":oct(member.mode),"mtime":member.mtime,"linkname":member.linkname,"size":member.size}
        if member.isfile():
            assert 0 < member.size < 300_000_000
            with tar.extractfile(member) as src:
                r["sha256"] = stream_hash(src)
        rows[name] = r
    return rows, duplicate


def inspect(path, kind, arch):
    if kind == "bz2":
        with tarfile.open(path,"r|bz2") as tar:
            rows, duplicate = headers(tar)
        argv = ["tar","-xjOf",path,"runsc"]
    else:
        with subprocess.Popen(["zstd","-dcq","--",path],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as dec:
            with tarfile.open(fileobj=dec.stdout,mode="r|") as tar:
                rows,duplicate = headers(tar)
            while dec.stdout.read(1024 * 1024):
                pass
            assert dec.wait() == 0
        argv = ["tar","--zstd","-xOf",path,"runsc"]
    with subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as pub:
        published_sha = stream_hash(pub.stdout)
        status = pub.wait()
    install = path.parent.parent / "release"
    normalized = {name.removeprefix("./"):r for name,r in rows.items() if "sha256" in r}
    staged = {n:q.digest(install/n) for n in q.MEMBERS}
    return {"archive_sha256":q.digest(path),"bytes":path.stat().st_size,"headers":rows,"duplicates":duplicate,
            "publisher_exit":status,"publisher_runsc_sha256":published_sha,
            "publisher_matches":status == 0 and published_sha == staged["runsc"],
            "stage_matches":normalized.keys() == q.MEMBERS and not duplicate and all(
                r["sha256"] == staged[n] and q.bins(install/n,q.TARGETS[arch][2]) for n,r in normalized.items())}


def root():
    roots = list((q.W/"home/.cache/bazel/_bazel_runner").glob("*/execroot/_main"))
    if len(roots) != 1:
        raise ValueError("expected one default Bazel execroot")
    return roots[0]


def original(arch):
    observed = q.record("native-"+arch)
    owner = observed.get("package_bep",{}).get(q.PACK,{})
    out = owner.get("output","")
    if observed.get("bazel_exit") or not owner.get("success") or not re.fullmatch(r"bazel-out/[^/]+/bin/debian/[^/]+[.]bz2",out):
        return False
    row = inspect(root()/out,"bz2",arch)
    row["source"] = q.TRIAL
    row["invocation"] = observed["invocation"]
    # Primary output, original spawn and downloaded bytes must identify the same archive.
    matches = [r for r in observed.get("package_spawns",[]) if r["owner"] == q.PACK and r["runner"] == "remote"
               and not r["cached"] and r["exit"] == 0 and r["worker"] == q.PROPS
               and any(o["sha256"] == row["archive_sha256"] and o["bytes"] == row["bytes"] and o["path"] == out for o in r["outputs"])]
    row["original_action_verified"] = len(matches) == 1
    q.save("legacy-headers-"+arch,row)
    return bool(matches and row["stage_matches"] and row["publisher_matches"])


def collect(arch):
    assert not (q.W/"remote.bazelrc").exists()
    name = "archive-"+arch
    data = q.record(name)
    data.update(observed={},accepted=False,other_failures=[],other_successful_spawns={k:0 for k in ("cache","empty_worker","other")})
    q.save(name,data)
    log = q.W/"data"/(name+".log")
    if data["bazel_exit"]:
        data["diagnostic"] = q.failure(log.read_text(errors="replace"))
    events = {}
    configs = {}
    bep = q.W/"data"/(name+".bep")
    if bep.exists():
        with bep.open() as f:
            for line in f:
                e = json.loads(line)
                if "started" in e:
                    data["bep_id"] = e["started"].get("uuid")
                if "finished" in e:
                    data["bep_exit"] = e["finished"].get("exitCode",{}).get("code",0)
                if "configuration" in e:
                    configs[e["id"]["configuration"]["id"]] = e["configuration"].get("cpu")
                id_ = e["id"].get("actionCompleted",{})
                if id_.get("label") in WRITERS and e.get("action",{}).get("type") == "Tar":
                    events[id_["label"]] = {"success":e["action"].get("success",False),"output":id_["primaryOutput"],
                                            "config":id_.get("configuration",{}).get("id")}
    data["bep_actions"] = events
    q.save(name,data)
    base = root()
    legacy_name = "legacy-headers-"+arch
    legacy = q.record(legacy_name) if (q.W/"public"/(legacy_name+".json")).exists() else {}
    for owner,ev in events.items():
        kind = WRITERS[owner]
        if not ev["success"] or not re.fullmatch(r"bazel-out/[^/]+/bin/debian/gvisor[.]tar[.]" + kind,ev["output"]):
            continue
        p = base/ev["output"]
        row = inspect(p,kind,arch)
        alias = p.parent/("gvisor-release-tar-"+kind+".tar."+kind)
        checked = observe_alias(alias,p,Path("/home/runner"),q.W/"home")
        row["legacy_link_observation"] = checked
        row["legacy_link"] = checked.get("kind") == "symlink" and checked["mapped_samefile"] and checked.get("mapped_sha256") == row["archive_sha256"]
        if legacy.get("original_action_verified"):
            row["different_headers"] = sorted(n for n in set(row["headers"])|set(legacy["headers"]) if row["headers"].get(n) != legacy["headers"].get(n))
        row["reference_available"] = bool(legacy.get("original_action_verified"))
        data["observed"][kind] = row
        q.save(name,data)
    compact = q.W/"data"/(name+".compact")
    if not compact.exists():
        return False
    actions = q.original_log(compact,data,owners=set(WRITERS))
    data["original_spawns"] = actions
    q.save(name,data)
    configured = q.record("tar-graph-"+arch)["actions"]
    identities = []
    for act in actions:
        owner = act["owner"]
        ev = events.get(owner,{})
        cfg = configured[owner]
        row = data["observed"].get(WRITERS[owner],{})
        args = cfg["args"]
        observed_tool = [f for f in act["tools"] if f["path"] == act["program"] and f["sha256"] == BSDTAR]
        compressor = [f for f in act["tools"] if f["path"].endswith("/tools/zstd/compressor")]
        if WRITERS[owner] == "zstd":
            mode = "--use-compress-program" in args and len(compressor) == 1
        else:
            mode = "--bzip2" in args and "--use-compress-program" not in args
        identities.append(bool(observed_tool and mode and args == act["argv"] and args[0] == act["program"] and ev.get("success")
                          and ev.get("config") == cfg["config"] and configs.get(ev.get("config")) == q.TARGETS[arch][1]
                          and act["worker"] == q.PROPS and act["runner"] == "remote" and not act["cached"] and not act["error"]
                          and act["exit"] == 0 and len(act["outputs"]) == 1 and act["outputs"][0]["path"] == ev.get("output")
                          and act["outputs"][0]["sha256"] == row.get("archive_sha256") and act["outputs"][0]["bytes"] == row.get("bytes")))
    data["native_shipping"] = (data.get("bep_id") == data["invocation"] and data.get("compact_invocation_matches")
                               and data.get("bep_exit") == data["bazel_exit"] == 0 and not data["other_failures"]
                               and not data["other_successful_spawns"]["other"] and len(actions) == 2
                               and {a["owner"] for a in actions} == set(WRITERS) and all(identities)
                               and set(data["observed"]) == set(KINDS)
                               and all(r["stage_matches"] and r["publisher_matches"] and r["legacy_link"] for r in data["observed"].values()))
    data["metadata_equivalent"] = (set(data["observed"]) == set(KINDS) and all(r["reference_available"] and r.get("different_headers") == []
                                  for r in data["observed"].values()))
    data["accepted"] = bool(data["native_shipping"] and data["metadata_equivalent"])
    q.save(name,data)
    print(name,"native shipping",data["native_shipping"],"metadata",data["metadata_equivalent"])
    return data["accepted"]


if __name__ == "__main__":
    op,*args = sys.argv[1:]
    if op == "graphs":
        ok = [graph(arch) for arch in ("amd64","arm64")]
    elif op == "variants":
        ok = [variants()]
    elif op == "build":
        build(*args)
        ok = [True]
    elif op == "collect":
        ok = [collect(*args)]
    elif op == "original":
        ok = [original(*args)]
    else:
        raise ValueError(op)
    sys.exit(not all(ok))
