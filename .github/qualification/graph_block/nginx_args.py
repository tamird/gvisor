"""Use the fork's published four-worker Nginx fixture on the graph index source pair."""

import sys

import pair


pair.ARMS = {
    "stock": "975fb172241c7946a9f67b4f52e0349d9240589a",
    "combined": "aed67b42bf0fb4e1f14cd7bfddaecd5f2e1d9a1d",
}
if sys.argv[1:2] == ["host"]:
    pair.nginx.host_caps(sys.argv[2])
elif sys.argv[1:] == ["build"]:
    pair.build()
else:
    assert sys.argv[1:] == ["measure"]
    pair.measure()
