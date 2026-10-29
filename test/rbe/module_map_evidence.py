"""Compare the original compiler-rt consumers and the generator's ELF on scratch."""
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess

from qualification_report import Owner, advertised_build_outputs

ROOT = Path(os.environ["RUNNER_TEMP"])
ARCH = os.environ["QUALIFICATION_ARCH"]
TARGET, MAP = os.environ["QUALIFICATION_TARGETS"].split()
IMAGE = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
REPORT = {"source": os.environ["QUALIFICATION_COMMIT"], "architecture": ARCH}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, detail):
    if not condition:
        raise ValueError(detail)


def spawns(log):
    data = subprocess.check_output([ROOT / "bb", "print", "--compact_execution_log=" + str(log)], text=True)
    decoder = json.JSONDecoder()
    i = 0
    while i < len(data):
        if data[i].isspace():
            i += 1
            continue
        row, i = decoder.raw_decode(data, i)
        yield row


def actual(row):
    props = {p["name"]: p["value"] for p in row["platform"]["properties"]}
    return (row.get("runner") == "remote" and not row.get("cacheHit")
            and not row.get("exitCode") and props.get("Arch") == ARCH
            and props.get("container-image") == IMAGE and props.get("OSFamily") == "linux")


def check_elf(data):
    require(data[:6] == b"\x7fELF\x02\x01", "generator ELF class")
    machine = struct.unpack_from("<H", data, 18)[0]
    require(machine == {"amd64": 62, "arm64": 183}[ARCH], "generator ELF ISA")
    off = struct.unpack_from("<Q", data, 32)[0]
    size, count = struct.unpack_from("<HH", data, 54)
    for i in range(count):
        typ, _, start, _, _, length, _, _ = struct.unpack_from("<IIQQQQQQ", data, off + i * size)
        require(typ != 3, "generator has PT_INTERP")
        if typ == 2:
            for j in range(start, start + length, 16):
                tag = struct.unpack_from("<q", data, j)[0]
                if tag == 0:
                    break
                require(tag != 1, "generator has DT_NEEDED")
    return machine


def check():
    (stage,) = (ROOT / "qualification").glob("invocation-*/events.jsonl")
    events = [json.loads(line) for line in stage.read_text().splitlines()]
    REPORT["invocation"] = next(e["started"]["uuid"] for e in events if "started" in e)
    REPORT["native_exit"] = int((stage.parent / "native-exit.txt").read_text())
    owners = {Owner.parse(e["id"]["targetCompleted"]) for e in events if "completed" in e
              and not e["id"]["targetCompleted"].get("aspect")
              and e["id"]["targetCompleted"]["label"] in (MAP, TARGET) and e["completed"].get("success")}
    require({o.label for o in owners} == {MAP, TARGET}, "default build owners")
    outputs = advertised_build_outputs(stage, {(o, "default") for o in owners})
    mmap, = (o for o in outputs if o["identity"]["label"] == MAP)
    archive, = (o for o in outputs if o["identity"]["label"] == TARGET and o["name"].endswith("libbuiltins.a"))
    rows = list(spawns(stage.parent / "execution.binpb"))
    m, = (r for r in rows if r.get("mnemonic") == "CppModuleMap" and r.get("targetLabel") == MAP
          and any(o["path"] == mmap["name"] for o in r.get("actualOutputs", [])))
    require(actual(m), "physical module-map action")
    program, output, param = m["commandArgs"]
    files = {v["path"]:v["digest"] for v in m["inputs"] if "digest" in v}
    tool = Path(program).read_bytes()
    require(digest(tool) == files[program]["hash"] and len(tool) == int(files[program]["sizeBytes"]), "executed generator digest")
    machine = check_elf(tool)
    links = [r["digest"]["hash"] for r in rows if r.get("mnemonic") == "CppLink" and actual(r)
             and any(x.get("digest", {}).get("hash") == digest(tool) for x in r.get("actualOutputs", []))]
    require(links, "generator physical CppLink")
    data = Path(output).read_bytes()
    require(output == mmap["name"] and digest(data) == mmap["sha256"] and len(data) == mmap["bytes"]
            and m["actualOutputs"][0]["digest"]["hash"] == mmap["sha256"], "module map output bytes")
    headers = [(name, int(size)) for name, size in re.findall(r'^  textual header "(.*?)" \{ size (\d+) \}$', data.decode(), re.M)]
    require(len(headers) == len(dict(headers)) and len(headers) > 1000, "textual header inventory")
    inventory = []
    for name, size in headers:
        require(int(files[name].get("sizeBytes", 0)) == size, "textual header size")
        inventory.append((name, files[name]["hash"], size))
    params = files[param.removeprefix("@")]
    fingerprint = digest(json.dumps(sorted(inventory), separators=(",", ":")).encode())
    REPORT["module_map"] = {"action":m["digest"]["hash"], "tool_path":program, "elf":digest(tool),
        "elf_size":len(tool), "machine":machine, "cpp_links":links, "headers":len(headers),
        "header_inventory":fingerprint, "parameters":params, "output":mmap["sha256"], "bytes":len(data)}
    if ARCH == "amd64":
        require((len(headers), fingerprint, params["hash"], mmap["sha256"], len(data)) ==
                (4526, "1382a6f7ce2d29c626e4a1021cb247111c5aaa2c653aba1b2d056b2cd6f091cb",
                 "c81c044cdf6c59b715b82ff48b6d1a0baf5ee7ee8773482cf085ac1895d07b59",
                 "2af3ef22afa5b73cbb9ede161e67f9be544e6850bdf170aa14841f27c0eb13c6", 624041),
                "original AMD64 input or reference output mismatch")
    compilations = [r for r in rows if r.get("mnemonic") == "CppCompile" and r.get("targetLabel") == TARGET
                    and any(x["path"] == output and x.get("digest",{}).get("hash") == mmap["sha256"] for x in r.get("inputs", []))]
    archives = [r for r in rows if r.get("mnemonic") == "CppArchive" and r.get("targetLabel") == TARGET
                and any(x["path"] == archive["name"] and x.get("digest",{}).get("hash") == archive["sha256"] for x in r.get("actualOutputs", []))]
    require(compilations and archives, "builtins consumes this map and produces its advertised archive")
    archive_data = Path(archive["name"]).read_bytes()
    require(archive_data.startswith(b"!<arch>") and digest(archive_data) == archive["sha256"], "builtins archive bytes")
    REPORT["builtins"] = {"compile_consumers":len(compilations), "first_compile":compilations[0]["digest"]["hash"],
        "archive":archive["sha256"], "bytes":len(archive_data), "archive_action":archives[0]["digest"]["hash"]}
    require(REPORT["native_exit"] == 0, "native Bazel exit")


if __name__ == "__main__":
    try:
        check()
        REPORT["verdict"] = "PASS"
    except Exception as error:
        REPORT["verdict"] = "INCOMPLETE"
        REPORT["error_type"] = type(error).__name__
        raise
    finally:
        (ROOT / "module-map.json").write_text(json.dumps(REPORT, indent=2) + "\n")
