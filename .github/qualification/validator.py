import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


def validate(root):
    expected = {
        f"{owner}_{variant}.xml"
        for owner in ("uname_test", "stat_times_test", "epoll_test")
        for variant in ("native", "runsc_systrap_directfs", "runsc_systrap_shared")
    }
    actual = {path.name for path in root.glob("*_test_*.xml")}
    assert actual == expected, (actual, expected)
    report = {}
    for name in sorted(expected):
        cases = ET.parse(root / name).getroot().findall(".//testcase")
        owner = name.split("_test_", 1)[0]
        names = {case.get("name") for case in cases}
        required = {
            "uname": {"Sanity", "SetNames", "UnprivilegedSetNames", "UnshareUTS"},
            "stat_times": {"FileRead"},
            "epoll": {"WatchOpenRandomDevice"},
        }[owner]
        assert names == required and len(cases) == len(required), (name, names)
        skipped = []
        for case in cases:
            assert case.get("classname") == {"uname": "UnameTest", "stat_times": "StatTimesTest", "epoll": "EpollTest"}[owner], (name, case.attrib)
            assert case.find("failure") is None and case.find("error") is None, (name, case.attrib)
            skip = case.find("skipped")
            if skip is not None:
                reason = " ".join(skip.itertext()) + " " + " ".join(skip.attrib.values())
                assert name == "uname_test_native.xml" and case.get("name") in {"SetNames", "UnshareUTS"}, (name, case.attrib, reason)
                assert "!ASSERT_NO_ERRNO_AND_VALUE(HaveCapability(CAP_SYS_ADMIN))" in reason, (name, case.attrib, reason)
                skipped.append({"name": case.get("name"), "reason": reason})
        report[name] = {"cases": len(cases), "passed": len(cases) - len(skipped), "skipped": skipped}
        print(name, len(cases) - len(skipped), "passed;", len(skipped), "explicit capability skips")
    (root / "xml-validation.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    validate(Path(sys.argv[1]))
