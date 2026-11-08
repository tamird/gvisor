"""Check the original allocator and protected-clear suites on their combined tree."""
import os
from pathlib import Path
import platform
import sys

# The workflow checks the original reader's blob before installing it here.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import original


def main():
    report = original.REPORT
    report.clear()
    report.update(carrier=os.environ["GITHUB_SHA"], source=original.git("rev-parse", "HEAD").strip(),
                  tree=original.git("write-tree").strip(), platform=platform.platform(),
                  erms="erms" in Path("/proc/cpuinfo").read_text().split(),
                  allocator="61470ea4162137e6871edd94e369f1c03592c163", parent=original.git("rev-parse", "HEAD^").strip(),
                  suites={}, errors={}, verified=False)
    original.save()
    assert report["tree"] == "34e7eb717acbadcea187c9cb4125864b6e3d9f03" and platform.machine() == "x86_64"
    restores = {operation + "/" + storage for operation in ("TestSaveToPreservesContents", "TestAsyncSaveToPreservesContents")
                for storage in ("memfd", "memfd_excluding_committed_zero_pages", "disk-backed")}
    safe = {"TestZeroOutSuccess", "TestZeroOutSegvError", "TestZeroOutBusError", "TestZeroOutLengths",
            "TestZeroOutLengthsFault", "TestZeroOutOtherERMS", "TestZeroOutOtherERMS/Lengths"}
    for group in ("TestZeroOutLengthsFault", "TestZeroOutOtherERMS/LengthsFault"):
        for signal in ("bus_error", "segmentation_fault"):
            for fault, size in (("0x8000", "0x10000"), ("0x2000000", "0x2100000")):
                safe.add(f"{group}/{signal}_at_{fault}_from_0x1_in_{size}")
    # The original merged fixture also tested #17204's discarded preallocation path.
    allocator_cases = original.ALLOC_CASES - {"TestZeroDiskBacked"}
    checks = (("pgalloc", original.ALLOC, allocator_cases | restores),
              ("safecopy", "//pkg/safecopy:safecopy_test", safe))
    for name, target, cases in checks:
        try:
            original.run(name, target, cases)
        except Exception as exc:
            report["errors"][name] = f"{type(exc).__name__}: {exc}"[:1200]
        original.save()
    report["verified"] = not report["errors"]
    original.save()
    return not report["verified"]


if __name__ == "__main__":
    raise SystemExit(main())
