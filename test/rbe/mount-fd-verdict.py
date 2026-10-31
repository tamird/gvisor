from pathlib import Path
from collections import Counter
import json
import sys
import xml.etree.ElementTree as XML

root=Path(sys.argv[1])
src="5c7696351adac2c56ac109c7e28d9018a604d8f6"
fd=set(['FsOpenTest.FsOpenSuccess', 'FsOpenTest.FsOpenInvalidFlags', 'FsOpenTest.FsOpenPermDenied', 'FsConfigTest.FsConfigSetSource', 'FsConfigTest.FsConfigSetParams', 'FsConfigTest.FsConfigSetSourceTwice', 'FsConfigTest.FsConfigCreateWithoutSource', 'FsConfigTest.FsConfigBadFilesystem', 'FsConfigTest.FsConfigCreateSuccess', 'FsConfigTest.FsConfigCreateTwice', 'FsConfigTest.FsConfigUnsupportedCmds', 'FsConfigTest.FsConfigReconfigureSucceeds', 'FsConfigTest.FsConfigUnsupportedTypes', 'FsMountTest.FsMountSuccess', 'FsMountTest.FsMountBeforeCreate', 'FsMountTest.FsMountTwice', 'FsMountTest.FsMountInvalidFlags', 'FsMountTest.FsMountDetachedUsage', 'MoveMountTest.MoveMountAttachSuccess', 'MoveMountTest.MoveMountFromAttached', 'MoveMountTest.MoveMountOntoDetached', 'MoveMountTest.MoveMountSubmountToFs', 'MoveMountTest.MoveMountWithoutEmptyPath', 'MoveMountTest.MoveMountLoop', 'OpenTreeTest.OpenTreeNoCloneSuccess', 'OpenTreeTest.OpenTreeCloneRequiresCapSysAdmin', 'OpenTreeTest.OpenTreeRecursiveRequiresClone', 'OpenTreeTest.OpenTreeInvalidFlags', 'OpenTreeTest.OpenTreeCloneNonRecursiveOnAttached', 'OpenTreeTest.OpenTreeCloneFailsOnDetachedMount', 'OpenTreeTest.OpenTreeCloneNonRecursiveOnDetached', 'OpenTreeTest.OpenTreeCloneRecursiveOnAttached', 'OpenTreeTest.OpenTreeCloneRecursiveOnDetached', 'OpenTreeTest.OpenTreeCloneAsOverlayLowerdir', 'OpenTreeTest.OpenTreeCloneFromAnotherUserNamespaceAsOverlayLowerdir', 'OpenTreeTest.OpenTreeCloneFile', 'MoveMountTest.MoveMountClonedFileOntoFile', 'MoveMountTest.MoveMountClonedProcFdFileOntoFile', 'MoveMountTest.MoveMountAttachedFileOntoFile', 'MoveMountTest.MoveMountFileOntoDirFails', 'MoveMountTest.MoveMountDirOntoFileFails'])
security={"MountTest.UserNamespaceProcFsmount", "MountTest.UserNamespaceProcFsmountRetryReadOnly","MountTest.UserNamespaceProcFsmountRevealingLockedMount"}
report={}
for lane in ("save","resume","guest-permissions","native-permissions"):
    d=root/lane
    expected=fd if lane in ("save","resume") else security
    evidence={}
    def value(name):
        return (d/name).read_text().strip() if (d/name).is_file() else None
    if (d/"test.xml").is_file():
        for t in XML.parse(d/"test.xml").iter("testcase"):
            name=t.get("classname","")+"."+t.get("name","")
            status="SKIP" if t.find("skipped") is not None or t.get("status")=="notrun" or t.get("result") in ("skipped","suppressed") else "FAIL" if len(t.findall("failure")+t.findall("error")) else "PASS"
            evidence.setdefault(name,[]).append(status)
    ok=value("source")==src and value("exit")=="0" and set(evidence)==expected and all(arms==["PASS"] for arms in evidence.values())
    report[lane]={"pass":ok,"source":value("source"),"bazel_exit":value("exit"),"target":value("target"),"fixture":value("fixture-sha256"),"missing":sorted(expected-set(evidence)),"unexpected":sorted(set(evidence)-expected),"cases":evidence}
    print(lane,ok,Counter(status for items in evidence.values() for status in items))
(root/"verdict.json").write_text(json.dumps(report,indent=2)+"\n")
if not all(r["pass"] for r in report.values()): raise SystemExit(1)
