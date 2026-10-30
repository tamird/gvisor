from pathlib import Path
import json
import os
import stat

def character(path):
    return path.exists() and stat.S_ISCHR(path.stat().st_mode)

control = Path("/dev/vfio/vfio")
nodes = [p.name for p in Path("/dev/vfio").glob("[0-9]*") if character(p)]
eligible = []
groups = list(Path("/sys/kernel/iommu_groups").glob("[0-9]*"))
pci = list(Path("/sys/bus/pci/devices").glob("*"))
tpus = []
for p in pci:
    vendor = p / "vendor"
    device = p / "device"
    if not vendor.is_file() or not device.is_file():
        continue
    if int(vendor.read_text(), 16) != 0x1ae0:
        continue
    if int(device.read_text(), 16) not in {0x5e, 0x56, 0x63, 0x62, 0x6f, 0x6e, 0x76, 0x75}:
        continue
    group = p / "iommu_group"
    driver = p / "driver"
    tpus.append(p.name)
    if group.is_dir() and driver.is_dir() and driver.resolve().name == "vfio-pci" and group.resolve().name in nodes:
        eligible.append({"group": group.resolve().name, "bdf": p.name})
print(json.dumps({"container": character(control), "group_nodes": sorted(nodes), "iommu_groups": len(groups), "pci_functions": len(pci), "allowed_tpus": sorted(tpus), "eligible_tpu_groups": eligible, "abi_executed": False}, sort_keys=False))
