"""Select only owners that lack a source-bound, successful original test."""
import hashlib
import json
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree


LABELS = (
    "//pkg/waiter:waiter_nogo",
    "//pkg/fdnotifier:fdnotifier_nogo",
    "//pkg/sentry/socket/unix/transport:transport_nogo",
    "//pkg/sentry/vfs:vfs_nogo",
    "//pkg/sentry/fsimpl/gofer:gofer_nogo",
    "//pkg/sentry/socket/hostinet:hostinet_nogo",
    "//pkg/sentry/fsimpl/eventfd:eventfd_nogo",
    "//pkg/sentry/socket/netstack:netstack_nogo",
    "//pkg/sentry/devices/tpuproxy/vfio:vfio_nogo",
)


def main():
    bundle, source, directory = sys.argv[1:]
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        if not any(name.startswith("main/") for name in names):
            raise ValueError("original bundle has no main records")
        for lane in ("analyzers", "arm"):
            results, missing = {}, []
            for label in LABELS:
                prefix = f"{lane}/{label.split(':')[1]}"
                name = f"{prefix}.json"
                if name in names:
                    row = json.loads(archive.read(name))
                    if (row["source"], row["lane"], row["target"]) != (source, lane, label):
                        raise ValueError(f"unexpected source for {name}")
                    if row.get("status") == "PASSED" and set(row.get("reports", {})) == {"test.log", "test.xml"}:
                        for kind, info in row["reports"].items():
                            data = archive.read(f"{prefix}.{kind}")
                            if hashlib.sha256(data).hexdigest() != info["hash"] or len(data) != info["size"]:
                                raise ValueError(f"report digest mismatch: {name} {kind}")
                        xml = ElementTree.fromstring(archive.read(f"{prefix}.test.xml"))
                        if not any(element.tag in ("failure", "error", "skipped") for element in xml.iter()):
                            results[label] = {
                                "invocation": row["invocation"],
                                "original_lane_exit": row["exit"],
                                "reports": row["reports"],
                            }
                            continue
                missing.append(label)
            target = Path(directory) / lane
            target.mkdir(parents=True, exist_ok=True)
            (target / "original.json").write_text(json.dumps(results, indent=2) + "\n")
            (target / "targets.txt").write_text("".join(f"{label}\n" for label in missing))
            print(f"{lane}: {len(results)} original passes reused; {len(missing)} missing")


if __name__ == "__main__":
    main()
