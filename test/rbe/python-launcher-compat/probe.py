import json
import sys
from pathlib import Path

print(json.dumps({"option":sys._xoptions.get("standalone"),"prefix":sys.prefix,"base_prefix":sys.base_prefix,"executable":sys.executable,"argv":sys.argv[1:],"payload":Path(__file__).with_name("payload.txt").read_text().strip()}))
