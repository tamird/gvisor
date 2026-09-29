// Copyright 2019 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package lib

import (
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
)

var nodejsTestRegEx = regexp.MustCompile(`^test-[^-].+\.js$`)

// Location of nodejs tests relative to working dir.
const nodejsTestDir = "test"

// nodejsRunner implements TestRunner for NodeJS.
type nodejsRunner struct{}

var _ TestRunner = nodejsRunner{}

// ListTests implements TestRunner.ListTests.
func (nodejsRunner) ListTests() ([]string, error) {
	testSlice, err := Search(nodejsTestDir, nodejsTestRegEx)
	if err != nil {
		return nil, err
	}
	return testSlice, nil
}

// inspectorFailureCapture is fork-only diagnostic instrumentation for the pinned
// Node 22.2.0 fixture. Await child stdio closure and retain its raw stderr before
// the existing expected-exit assertion can terminate the parent test process.
const inspectorFailureCapture = `import hashlib
from pathlib import Path

path = Path('test/parallel/test-inspector-debug-end.js')
contents = path.read_bytes()
digest = hashlib.sha256(contents).hexdigest()
diagnostic_digest = 'cf087f89eac1fd83845b37a59cee216e6712f61c0df814a13e9a3945b72671a6'
if digest != diagnostic_digest:
    assert digest == 'b9c43f55ab84e55857e39ca6389b892de3313e70bfc1bf776353b585df1f4acd', ('unexpected inspector fixture', digest)
    text = contents.decode()
    before = "  const instance = new NodeInstance('--inspect-brk=0', script);"
    after = r"""  const instance = new NodeInstance('--inspect-brk=0', script);
  const childStderr = [];
  instance._process.stderr.on('data', (chunk) => {
    childStderr.push(Buffer.from(chunk));
  });
  const closed = new Promise((resolve) => {
    instance._process.once('close', resolve);
  });"""
    assert text.count(before) == 1, ("inspector replacement count", text.count(before))
    text = text.replace(before, after)
    before = r"""  strictEqual((await instance.expectShutdown()).exitCode, 42);
}

async function runTest()"""
    after = r"""  const result = await instance.expectShutdown();
  await closed;
  if (result.exitCode !== 42) {
    require('fs').writeFileSync(2, Buffer.concat([
      Buffer.from('INSPECTOR_CHILD_STDERR_BEGIN\n'),
      ...childStderr,
      Buffer.from('\nINSPECTOR_CHILD_STDERR_END\n'),
    ]));
  }
  strictEqual(result.exitCode, 42);
}

async function runTest()"""
    assert text.count(before) == 1, ("inspector replacement count", text.count(before))
    text = text.replace(before, after)
    contents = text.encode()
    assert hashlib.sha256(contents).hexdigest() == diagnostic_digest
    path.write_bytes(contents)
print(f'Inspector diagnostic fixture SHA256: {diagnostic_digest}', flush=True)
`

// TestCmds implements TestRunner.TestCmds.
func (nodejsRunner) TestCmds(tests []string) []*exec.Cmd {
	args := append([]string{filepath.Join("tools", "test.py"), "--timeout=180"}, tests...)
	cmd := exec.Command("/usr/bin/python3.10", args...)
	if slices.Contains(tests, "parallel/test-inspector-debug-end.js") {
		return []*exec.Cmd{exec.Command("/usr/bin/python3.10", "-c", inspectorFailureCapture), cmd}
	}
	return []*exec.Cmd{cmd}
}
