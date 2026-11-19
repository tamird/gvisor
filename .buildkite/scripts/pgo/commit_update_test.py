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
    return self.git_bytes(directory, *args).decode().strip()

  def git_bytes(self, directory: Path, *args: str) -> bytes:
    result = subprocess.run(
        [self.git_binary, *args],
        cwd=directory,
        env=self.env,
        capture_output=True,
        timeout=10,
        check=True,
    )
    return result.stdout

  def write(self, directory: Path, name: str, contents: str | bytes) -> None:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(contents, str):
      path.write_text(contents)
    else:
      path.write_bytes(contents)

  def publish_result(self) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
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

  def publish(self) -> None:
    result = self.publish_result()
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
    contents = b"\x00new profile\xff \t\n\n"
    self.write(self.work, "runsc/profiles/data/new/profile.pgo", contents)
    self.publish()
    branch = self.published_branch()
    self.assertEqual(
        self.git_bytes(self.remote, "show", branch + ":runsc/profiles/data/new/profile.pgo"),
        contents,
    )

  def test_modified_profiles_are_published(self) -> None:
    contents = b"\x00updated profile\xff \t\n\n"
    self.write(self.work, "runsc/profiles/data/tracked.pgo", contents)
    self.publish()
    branch = self.published_branch()
    self.assertEqual(
        self.git_bytes(self.remote, "show", branch + ":runsc/profiles/data/tracked.pgo"),
        contents,
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

  def assert_already_published(self, name: str) -> None:
    contents = b"\x00same profile\xff \t\n\n"
    self.write(self.work, name, contents)
    self.write(self.seed, name, contents)
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

  def test_already_published_profiles_do_not_create_pr(self) -> None:
    self.assert_already_published("runsc/profiles/data/new/profile.pgo")

  def test_already_published_modified_profiles_do_not_create_pr(self) -> None:
    self.assert_already_published("runsc/profiles/data/tracked.pgo")

  def test_unchanged_profiles_do_not_create_pr(self) -> None:
    original = self.git(self.work, "rev-parse", "HEAD")
    self.publish()
    self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), original)
    self.assertFalse(self.calls.exists(), "Unchanged profiles created a PR")
    self.assertEqual(
        self.git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads"),
        "refs/heads/master",
    )

  def test_conflicting_profiles_are_preserved_without_publishing(self) -> None:
    name = "runsc/profiles/data/tracked.pgo"
    generated = b"\x00generated profile\xff\n"
    upstream = b"\x00concurrent profile\xfe\n"
    self.write(self.work, name, generated)
    self.write(self.seed, name, upstream)
    self.git(self.seed, "add", name)
    self.git(self.seed, "commit", "-m", "Publish a different profile")
    self.git(self.seed, "push", "origin", "master")
    result = self.publish_result()
    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
    self.assertEqual(self.git(self.work, "status", "--porcelain"), "UU " + name)
    self.assertFalse(self.calls.exists(), "Conflicting profiles created a PR")
    self.assertEqual(
        self.git_bytes(self.work, "show", "pgo/update-2026-01-01:" + name),
        generated,
    )
    self.assertEqual(self.git_bytes(self.remote, "show", "master:" + name), upstream)
    self.assertEqual(
        self.git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads"),
        "refs/heads/master",
    )

  def test_comparison_error_does_not_publish(self) -> None:
    name = "runsc/profiles/data/new/profile.pgo"
    contents = b"\x00generated profile\xff\n"
    self.write(self.work, name, contents)
    wrapper = self.root / "bin/git"
    wrapper.write_text(
        '#!/bin/bash\n'
        'if [[ "$#" == 4 && "$1" == diff && "$2" == --quiet && '
        '"$3" == FETCH_HEAD && "$4" == HEAD ]]; then exit 128; fi\n'
        'exec "$PGO_TEST_REAL_GIT" "$@"\n'
    )
    wrapper.chmod(0o755)
    self.env["PGO_TEST_REAL_GIT"] = self.git_binary
    result = self.publish_result()
    self.assertEqual(result.returncode, 128, result.stdout + result.stderr)
    self.assertFalse(self.calls.exists(), "A failed comparison created a PR")
    self.assertEqual(
        self.git_bytes(self.work, "show", "pgo/update-2026-01-01:" + name),
        contents,
    )
    self.assertEqual(
        self.git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads"),
        "refs/heads/master",
    )


if __name__ == "__main__":
  unittest.main()
