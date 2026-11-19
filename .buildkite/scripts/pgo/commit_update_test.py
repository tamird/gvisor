# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Exercises the PGO publisher with local Git remotes and no GitHub access."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class CommitUpdateTest(unittest.TestCase):

  def setUp(self) -> None:
    temporary = tempfile.TemporaryDirectory(dir=os.environ["TEST_TMPDIR"])
    self.addCleanup(temporary.cleanup)
    self.root = Path(temporary.name)
    self.remote = self.root / "remote.git"
    self.seed = self.root / "seed"
    self.work = self.root / "work"
    self.calls = self.root / "gh-calls"
    git_binary = shutil.which("git")
    if git_binary is None:
      self.fail("The local worker must provide Git")
    self.git_binary = git_binary
    self.publisher = (
        Path(os.environ["TEST_SRCDIR"])
        / os.environ["TEST_WORKSPACE"]
        / ".buildkite/scripts/pgo/publish.sh"
    )
    self.assertTrue(self.publisher.is_file(), self.publisher)
    commands = self.root / "bin"
    commands.mkdir()
    gh = commands / "gh"
    gh.write_text(
        '#!/bin/bash\n'
        'case "$1" in\n'
        '  --version|auth) exit 0 ;;\n'
        '  pr) printf "%s\\n" "$*" >> "$PGO_TEST_GH_CALLS" ;;\n'
        '  *) exit 2 ;;\n'
        'esac\n'
    )
    gh.chmod(0o755)
    config = self.root / "gitconfig"
    config.write_text(
        '[user]\n  name = PGO test\n  email = pgo-test@example.invalid\n'
        '[commit]\n  gpgSign = false\n'
        f'[url "{self.remote}"]\n'
        '  insteadOf = https://github.com/gvisor/gvisor.git\n'
    )
    self.env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    self.env.update({
        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "file",
        "PGO_TEST_GH_CALLS": str(self.calls),
    })
    self.git(self.root, "init", "--bare", "--initial-branch=master", str(self.remote))
    self.git(self.root, "init", "--initial-branch=master", str(self.seed))
    self.write(self.seed, "runsc/profiles/data/tracked.pgo", "old profile\n")
    self.git(self.seed, "add", ".")
    self.git(self.seed, "commit", "-m", "Initial profiles")
    self.git(self.seed, "remote", "add", "origin", str(self.remote))
    self.git(self.seed, "push", "origin", "master")
    self.git(self.root, "clone", str(self.remote), str(self.work))

  def git(self, directory: Path, *args: str) -> str:
    result = subprocess.run(
        [self.git_binary, *args],
        cwd=directory,
        env=self.env,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return result.stdout.strip()

  def write(self, directory: Path, name: str, contents: str) -> None:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)

  def publish(self) -> None:
    result = subprocess.run(
        [
            "/bin/bash",
            str(self.publisher),
            "2026-01-01",
            "https://github.com/gvisor/gvisor.git",
            "pgo/update-2026-01-01",
        ],
        cwd=self.work,
        env=self.env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    self.assertEqual(self.git(self.work, "status", "--porcelain"), "")

  def published_branch(self) -> str:
    branch = self.git(self.work, "branch", "--show-current")
    self.assertTrue(branch.startswith("pgo/update-"), branch)
    self.assertEqual(
        self.git(self.remote, "rev-parse", "refs/heads/" + branch),
        self.git(self.work, "rev-parse", "HEAD"),
    )
    calls = self.calls.read_text().splitlines()
    self.assertEqual(len(calls), 1, calls)
    self.assertIn(" create ", " " + calls[0] + " ")
    return branch

  def test_new_profiles_are_published(self) -> None:
    self.write(self.work, "runsc/profiles/data/new/profile.pgo", "new profile\n")
    self.publish()
    branch = self.published_branch()
    self.assertEqual(
        self.git(self.remote, "show", branch + ":runsc/profiles/data/new/profile.pgo"),
        "new profile",
    )

  def test_modified_profiles_are_published(self) -> None:
    self.write(self.work, "runsc/profiles/data/tracked.pgo", "updated profile\n")
    self.publish()
    branch = self.published_branch()
    self.assertEqual(
        self.git(self.remote, "show", branch + ":runsc/profiles/data/tracked.pgo"),
        "updated profile",
    )

  def test_profiles_rebase_onto_concurrent_master(self) -> None:
    self.write(self.work, "runsc/profiles/data/new/profile.pgo", "new profile\n")
    self.write(self.seed, "unrelated.txt", "concurrent change\n")
    self.git(self.seed, "add", "unrelated.txt")
    self.git(self.seed, "commit", "-m", "Concurrent master update")
    self.git(self.seed, "push", "origin", "master")
    master = self.git(self.seed, "rev-parse", "HEAD")
    self.publish()
    branch = self.published_branch()
    self.assertEqual(self.git(self.remote, "rev-parse", branch + "^"), master)
    self.assertEqual(
        self.git(self.remote, "show", branch + ":unrelated.txt"),
        "concurrent change",
    )
    self.assertEqual(
        self.git(self.remote, "show", branch + ":runsc/profiles/data/new/profile.pgo"),
        "new profile",
    )

  def test_already_published_profiles_do_not_create_pr(self) -> None:
    name = "runsc/profiles/data/new/profile.pgo"
    self.write(self.work, name, "new profile\n")
    self.write(self.seed, name, "new profile\n")
    self.git(self.seed, "add", name)
    self.git(self.seed, "commit", "-m", "Publish the same generated profile")
    self.git(self.seed, "push", "origin", "master")
    master = self.git(self.seed, "rev-parse", "HEAD")
    self.publish()
    self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), master)
    self.assertFalse(self.calls.exists(), "An already-published profile created a PR")
    self.assertEqual(
        self.git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads"),
        "refs/heads/master",
    )


if __name__ == "__main__":
  unittest.main()
