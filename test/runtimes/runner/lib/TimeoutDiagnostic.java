// Copyright 2026 The gVisor Authors.
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

import com.sun.javatest.regtest.exec.DefaultTimeoutHandler;
import java.io.File;
import java.io.IOException;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.lang.management.ManagementFactory;
import java.lang.management.ThreadInfo;
import java.nio.file.Files;
import java.nio.file.NoSuchFileException;
import java.nio.file.Path;
import java.time.Instant;
import java.util.Arrays;

/** Fork-only observation of jtreg's process/output wait, before its child jstack. */
public class TimeoutDiagnostic extends DefaultTimeoutHandler {
    // jtreg c40e6ea6d requires this constructor, including File rather than Path.
    public TimeoutDiagnostic(PrintWriter log, File outputDir, File testJdk) {
        super(log, outputDir, testJdk.toPath());
        log.println("TIMEOUT_DIAGNOSTIC_LOADED parent_pid=" + ProcessHandle.current().pid());
        log.flush();
    }

    @Override
    protected void runActions(Process child, long pid) throws InterruptedException {
        log.println("TIMEOUT_DIAGNOSTIC_BEGIN time=" + Instant.now()
                + " parent_pid=" + ProcessHandle.current().pid() + " child_pid=" + pid);
        // These are the parent JVM's threads: the action, both StreamCopiers,
        // and the native process reaper. The default handler only dumps the child.
        for (ThreadInfo thread : ManagementFactory.getThreadMXBean().dumpAllThreads(true, true)) {
            log.println("THREAD name=" + thread.getThreadName() + " id=" + thread.getThreadId()
                    + " state=" + thread.getThreadState() + " lock=" + thread.getLockInfo()
                    + " lock_owner=" + thread.getLockOwnerName());
            for (StackTraceElement frame : thread.getStackTrace()) {
                log.println("    at " + frame);
            }
            log.println("    monitors=" + Arrays.toString(thread.getLockedMonitors()));
            log.println("    synchronizers=" + Arrays.toString(thread.getLockedSynchronizers()));
        }
        log.println("CHILD alive=" + child.isAlive());
        try {
            log.println("CHILD exit_value=" + child.exitValue());
        } catch (IllegalThreadStateException e) {
            log.println("CHILD exit_value=not_available");
        }
        try {
            log.println("CHILD proc_status=" + Files.readString(Path.of("/proc", Long.toString(pid), "status")));
        } catch (NoSuchFileException e) {
            log.println("CHILD proc_status=absent");
        } catch (IOException e) {
            log.println("CHILD proc_status_error=" + e);
        }
        log.flush();
        super.runActions(child, pid);
        log.println("TIMEOUT_DIAGNOSTIC_END time=" + Instant.now());
        log.flush();
    }

    /** Verifies capture mechanics separately from jtreg invoking the handler. */
    public static void main(String[] args) throws Exception {
        PrintWriter log = new PrintWriter(System.out, true);
        log.println("TIMEOUT_DIAGNOSTIC_PREFLIGHT_BEGIN");
        File jdk = new File(System.getProperty("java.home"));
        Process child = new ProcessBuilder(new File(jdk, "bin/java").toString(), "-version")
                .inheritIO().start();
        try {
            if (child.waitFor() != 0) {
                throw new AssertionError("preflight child failed");
            }
            StringWriter capture = new StringWriter();
            TimeoutDiagnostic handler = TimeoutDiagnostic.class
                    .getConstructor(PrintWriter.class, File.class, File.class)
                    .newInstance(new PrintWriter(capture), new File("."), jdk);
            handler.runActions(child, child.pid());
            String output = capture.toString();
            log.print(output);
            log.flush();
            for (String expected : new String[] {
                    "TIMEOUT_DIAGNOSTIC_LOADED", "THREAD name=main ",
                    "at TimeoutDiagnostic.runActions(", "CHILD alive=false",
                    "CHILD exit_value=0", "CHILD proc_status=absent", "TIMEOUT_DIAGNOSTIC_END"
            }) {
                if (!output.contains(expected)) {
                    throw new AssertionError("preflight missing: " + expected);
                }
            }
            log.println("TIMEOUT_DIAGNOSTIC_PREFLIGHT_PASS");
        } finally {
            child.destroyForcibly();
            child.waitFor();
        }
    }
}
