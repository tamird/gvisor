"""Read the original qualification ZIP from stdin and check each observed arm."""

import hashlib
import io
import json
import sys
import zipfile


DIGEST = "57f39045ad8b19ad963309ac983d2d8d201a3956f907b289d3ed3c713c14852e"
PRODUCT = "2d4356b749b34804db0a6aff306724d2cb9aff2a"
BASE = "9ee2906f518e1d0f9fd109e9e1feb2812096418b"
TEST = "//pkg/tcpip/transport/tcp/test/e2e:tcp_test"
CASE = "TestConnectICMPError"
BLOBS = {
    "runtime": "936d3940da835d5d8e5dc663d9d88518e0740735",
    "concrete_locked": "936d3940da835d5d8e5dc663d9d88518e0740735",
    "concrete_unlocked": "b52ef67179bbb48617f03cc3d29c41f49be51f46",
    "original_interface": "8ca4aa54820c4869ed9aa36677015e1b89a18d23",
}


def readback(raw):
    assert hashlib.sha256(raw).hexdigest() == DIGEST
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        assert source.namelist() == ["result.json"]
        evidence = json.loads(source.read("result.json"))
    assert evidence["verified"] is False and evidence["product"] == PRODUCT and evidence["parent"] == BASE
    assert set(evidence["arms"]) == set(BLOBS)
    for arm, row in evidence["arms"].items():
        negative = arm == "concrete_unlocked"
        assert row["source"] == (BASE if arm == "original_interface" else PRODUCT)
        assert row["testBlob"] == BLOBS[arm] and row["verified"] == (not negative)
        assert row["attempts"] == 1 and row["strategy"] == "local" and row["cached"] is False
        assert row["status"] == ("FAILED" if negative else "PASSED")
        assert row["exit"] == (3 if negative else 0) and row["finished"] == [row["exit"]]
        if arm == "runtime":
            assert row["owner"] == TEST and row["namedXML"] and row["xmlClean"] and row["xmlCases"] == 1
            assert row["goRuns"] == [CASE] and row["goResults"] == [["PASS", CASE]]
            continue
        assert row["owner"] == TEST + "_nogo"
        assert sorted(row["sections"]) == [[a, "" if negative else "PASS"] for a in ("amd64", "arm64")]
        assert set(row["findings"]) == {"amd64", "arm64"}
        for finding in row["findings"].values():
            assert finding["count"] == finding["matching"]
            if negative:
                assert row["callLine"] == 387 and finding["matching"] > 0
            else:
                assert finding["matching"] == 0
    return {"originalArchive": "https://github.com/tamird/gvisor/actions/runs/42385235839/artifacts/13579008321",
            "archiveSHA256": DIGEST, "product": PRODUCT, "base": BASE,
            "qualification": "a1fd0bc2f743422a385a88424ab55d170f34fe63",
            "originalAggregate": evidence["verified"], "controlSatisfied": True,
            "arms": evidence["arms"]}


if __name__ == "__main__":
    print(json.dumps(readback(sys.stdin.buffer.read()), indent=2))
