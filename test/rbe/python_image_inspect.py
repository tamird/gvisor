"""Verify inherited Python layers and the existing HTTP-server command."""
import json
import os
from pathlib import Path
import sys

from upstream_image_inspect import run

os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"])
config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
run(Path(sys.argv[1]), config, package="//images/basic:", name="python",
    image_name="basic/python", upstream=Path(__file__).with_name("python_upstream"),
    prefix="python", command_overrides={
        "Entrypoint": ["python", "-m", "http.server", "8080"], "Cmd": None})
