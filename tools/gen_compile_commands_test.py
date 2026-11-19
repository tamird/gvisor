# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests the compiler argument boundary of Bazel's aquery output."""

import json
import unittest
from unittest import mock

import gen_compile_commands


class CompileArgumentsTest(unittest.TestCase):

  def test_arguments(self) -> None:
    arguments = ["/usr/bin/gcc", "-Iinclude path", "-c", "pkg/source.cc"]
    response = {"actions": [{"arguments": arguments, "mnemonic": "CppCompile"}]}
    with mock.patch.object(
        gen_compile_commands, "_bazel", return_value=json.dumps(response)
    ) as run:
      self.assertEqual(
          gen_compile_commands._compile_arguments(
              ["bazel"], ["--config=x86_64"], ["//pkg:owner"]
          ),
          [arguments],
      )
      self.assertEqual(
          run.call_args.args[:3], (["bazel"], ["--config=x86_64"], "aquery")
      )
      self.assertEqual(
          run.call_args.args[-1], 'mnemonic("CppCompile", deps(//pkg:owner))'
      )

  def test_no_actions(self) -> None:
    with mock.patch.object(
        gen_compile_commands, "_bazel", return_value='{"actions": []}'
    ):
      self.assertEqual(
          gen_compile_commands._compile_arguments(["bazel"], [], ["//pkg:owner"]), []
      )

  def test_invalid_arguments(self) -> None:
    for response in ([], {}, {"actions": {}}, {"actions": [None]},
                     {"actions": [{}]}, {"actions": [{"arguments": "gcc -c source.cc"}]},
                     {"actions": [{"arguments": ["gcc", 1]}]}):
      with self.subTest(response=response), mock.patch.object(
          gen_compile_commands, "_bazel", return_value=json.dumps(response)
      ):
        with self.assertRaises(ValueError):
          gen_compile_commands._compile_arguments(["bazel"], [], ["//pkg:owner"])


if __name__ == "__main__":
  unittest.main()
