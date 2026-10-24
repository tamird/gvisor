"""Verify both imported MySQL configs without OCI reserialization."""
import json
import os
from pathlib import Path
import sys

from upstream_image_inspect import run

os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"])
config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
run(Path(sys.argv[1]), config, package="//images/basic:", name="mysql",
    image_name="basic/mysql", upstream=Path(__file__).with_name("mysql_upstream"),
    prefix="mysql", preserve_config=True)
