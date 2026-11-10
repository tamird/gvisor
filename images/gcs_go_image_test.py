import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class CompilerTest(unittest.TestCase):
    def test_packaged_gcsfuse(self) -> None:
        source = Path("images/fuse/gcs")
        with tempfile.TemporaryDirectory(dir=os.environ["TEST_TMPDIR"]) as temp:
            work = Path(temp)
            for name in ("Dockerfile", "gcsfuse_test.sh"):
                shutil.copy2(source / name, work / name)
            builder = (work / "Dockerfile").read_text().splitlines()[0].split()[1]
            subprocess.run(["docker", "build", "--iidfile", str(work / "image"), str(work)], check=True)
            image = (work / "image").read_text().strip()
            version = subprocess.check_output(
                ["docker", "run", "--rm", "--entrypoint", "/usr/local/bin/gcsfuse", image, "--version"], text=True
            )
            print(version, flush=True)
            self.assertIn("gcsfuse", version.lower())
            container = subprocess.check_output(["docker", "create", image], text=True).strip()
            try:
                subprocess.run(["docker", "cp", container + ":/usr/local/bin/gcsfuse", str(work / "gcsfuse")], check=True)
            finally:
                subprocess.run(["docker", "rm", container], check=True)
            metadata = subprocess.check_output(
                ["docker", "run", "--rm", "--entrypoint", "/usr/local/go/bin/go",
                 "-v", str(work / "gcsfuse") + ":/gcsfuse:ro", builder, "version", "-m", "/gcsfuse"], text=True
            )
            print(metadata, flush=True)
            self.assertEqual(metadata.splitlines()[0], "/gcsfuse: go1.27.4")
            self.assertIn("github.com/googlecloudplatform/gcsfuse/v2", metadata)
            self.assertIn("GOARCH=amd64", metadata)


if __name__ == "__main__":
    unittest.main()
