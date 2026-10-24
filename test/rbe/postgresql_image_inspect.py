"""Verify both imported PostgreSQL configs without OCI reserialization."""
import json
import os
from pathlib import Path
import sys

from upstream_image_inspect import run

os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"])
config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
run(Path(sys.argv[1]), config, package="//images/compatibility:", name="postgresql",
    image_name="compatibility/postgresql/postgresql", upstream=Path(__file__).with_name("postgresql_upstream"),
    prefix="postgresql", preserve_config=True)
