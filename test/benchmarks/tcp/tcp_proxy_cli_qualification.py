"""Private executable-level TCP proxy qualification; no parser replacement."""

import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
import xml.etree.ElementTree as ET


BINARY = Path(sys.argv.pop(1)).resolve(strict=True)
OBSERVATIONS: list[dict] = []


def identity(path: Path) -> dict:
    data = path.read_bytes()
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


class ProxyCLI(unittest.TestCase):
    def rejected(self, args: list[str], expected: str) -> None:
        with tempfile.TemporaryDirectory(prefix="tcp-proxy-cli-") as directory:
            profile = Path(directory) / "early-profile"
            command = [str(BINARY), *args, "-cpuprofile=" + str(profile)]
            started = time.monotonic()
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            elapsed = time.monotonic() - started
            row = {"case": self._testMethodName, "argv": command, "exit": result.returncode,
                   "stdout": result.stdout, "stderr": result.stderr,
                   "elapsed_seconds": elapsed, "profile_created": profile.exists()}
            OBSERVATIONS.append(row)
            self.assertEqual(result.returncode, 1, row)
            self.assertIn(expected, result.stderr)
            self.assertNotIn("connect failed retrying", result.stderr)
            self.assertNotIn("netstack error:", result.stderr)
            self.assertFalse(profile.exists(), row)

    def test_listen_overflow_before_setup(self) -> None:
        self.rejected(["-port=65536", "-forward=127.0.0.1:80", "-client",
                       "-addr=10.0.0.1", "-iface=qualification-missing"],
                      "invalid listen port:")

    def test_forward_overflow_before_setup(self) -> None:
        self.rejected(["-port=12345", "-forward=127.0.0.1:65536", "-client",
                       "-addr=10.0.0.1", "-iface=qualification-missing"],
                      "invalid forwarding target:")

    def test_native_forward_overflow_before_setup(self) -> None:
        self.rejected(["-port=12345", "-forward=127.0.0.1:65537"],
                      "invalid forwarding port:")

    def test_valid_forward_reaches_setup(self) -> None:
        command = [str(BINARY), "-port=12345", "-forward=127.0.0.1:80", "-client",
                   "-addr=10.0.0.1", "-iface=qualification-missing"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        row = {"case": self._testMethodName, "argv": command, "exit": result.returncode,
               "stdout": result.stdout, "stderr": result.stderr}
        OBSERVATIONS.append(row)
        self.assertEqual(result.returncode, 1, row)
        self.assertIn("netstack error: failed to find interface: qualification-missing", result.stderr)
        self.assertNotIn("invalid forwarding target:", result.stderr)
        self.assertNotIn("connect failed retrying", result.stderr)

    def test_native_forwarding_hex_listen(self) -> None:
        with socket.socket() as backend, socket.socket() as reservation:
            backend.bind(("127.0.0.1", 0))
            backend.listen(4)
            backend.settimeout(10)
            reservation.bind(("127.0.0.1", 0))
            listen_port = reservation.getsockname()[1]
            reservation.close()
            command = [str(BINARY), "-port=" + hex(listen_port),
                       "-forward=127.0.0.1:" + str(backend.getsockname()[1])]
            row = {"case": self._testMethodName, "argv": command, "listen_port": listen_port}
            OBSERVATIONS.append(row)
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
            stderr = bytearray()
            try:
                self.assertIsNotNone(process.stderr)
                os.set_blocking(process.stderr.fileno(), False)
                deadline = time.monotonic() + 10
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stderr, selectors.EVENT_READ)
                    while b"client=false, server=false, ready." not in stderr:
                        remaining = deadline - time.monotonic()
                        self.assertGreater(remaining, 0, bytes(stderr))
                        self.assertIsNone(process.poll(), bytes(stderr))
                        for key, _ in selector.select(remaining):
                            chunk = os.read(key.fd, 65536)
                            self.assertTrue(chunk, bytes(stderr))
                            stderr.extend(chunk)
                os.set_blocking(process.stderr.fileno(), True)
                upstream, _ = backend.accept()
                with upstream, socket.create_connection(("127.0.0.1", listen_port), timeout=10) as downstream:
                    upstream.settimeout(10)
                    payload = bytes(range(256)) * 257
                    response = b"response:" + payload[::-1]
                    downstream.sendall(payload)
                    received = bytearray()
                    while len(received) < len(payload):
                        chunk = upstream.recv(len(payload) - len(received))
                        self.assertTrue(chunk)
                        received.extend(chunk)
                    self.assertEqual(bytes(received), payload)
                    upstream.sendall(response)
                    returned = bytearray()
                    while len(returned) < len(response):
                        chunk = downstream.recv(len(response) - len(returned))
                        self.assertTrue(chunk)
                        returned.extend(chunk)
                    self.assertEqual(bytes(returned), response)
                    row.update(sent_bytes=len(payload), returned_bytes=len(response),
                               payload_sha256=hashlib.sha256(received).hexdigest(),
                               response_sha256=hashlib.sha256(returned).hexdigest())
                process.send_signal(signal.SIGTERM)
                stdout, tail = process.communicate(timeout=10)
                stderr.extend(tail)
                row.update(exit=process.returncode, stdout=stdout.decode(), stderr=stderr.decode())
                self.assertEqual(process.returncode, 0, row)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    stdout, tail = process.communicate(timeout=10)
                    stderr.extend(tail)
                    row.update(cleanup_killed=True, exit=process.returncode,
                               stdout=stdout.decode(), stderr=stderr.decode())


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = ET.Element("testsuite", name="tcp_proxy_cli")

    def startTest(self, test):
        super().startTest(test)
        self.case = ET.SubElement(self.cases, "testcase", classname="ProxyCLI", name=test._testMethodName)
        self.started = time.monotonic()

    def stopTest(self, test):
        self.case.set("time", str(time.monotonic() - self.started))
        super().stopTest(test)

    def addFailure(self, test, error):
        ET.SubElement(self.case, "failure").text = "".join(traceback.format_exception(*error))
        super().addFailure(test, error)

    def addError(self, test, error):
        ET.SubElement(self.case, "error").text = "".join(traceback.format_exception(*error))
        super().addError(test, error)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ProxyCLI)
    result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    result.cases.set("tests", str(result.testsRun))
    result.cases.set("failures", str(len(result.failures)))
    result.cases.set("errors", str(len(result.errors)))
    xml_path = Path(os.environ["XML_OUTPUT_FILE"])
    xml_path.write_bytes(ET.tostring(result.cases, encoding="utf-8", xml_declaration=True))
    print("TCP_PROXY_CLI_OBSERVATIONS=" + json.dumps({"binary": identity(BINARY),
        "observations": OBSERVATIONS, "success": result.wasSuccessful()}, sort_keys=True))
    raise SystemExit(0 if result.wasSuccessful() else 1)
