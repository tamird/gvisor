"""Authenticate the unchanged official Redis image and Make contexts."""
import json
import os
from pathlib import Path
import sys

from upstream_image_inspect import run

os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"])
config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
run(Path(sys.argv[1]), config, package="//images/benchmarks:", name="redis",
    image_name="benchmarks/redis", upstream=Path(__file__).with_name("redis_upstream"),
    prefix="redis", expected_config_ids=config["basic_redis_config_ids"])
