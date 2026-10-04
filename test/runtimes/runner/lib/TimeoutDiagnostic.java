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
import java.nio.channels.Pipe;
import java.nio.charset.StandardCharsets;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.NoSuchFileException;
import java.nio.file.Path;
import java.time.Instant;
import java.util.Arrays;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.FutureTask;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

/** Fork-only observation of jtreg's process/output wait, before its child jstack. */
public class TimeoutDiagnostic extends DefaultTimeoutHandler {
    private static final int MAX_PROCESSES = 128;
    private static final int MAX_DESCRIPTORS = 4096;
    private static final int MAX_PROC_BYTES = 4096;

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
        snapshotPipes();
        log.flush();
        super.runActions(child, pid);
        log.println("TIMEOUT_DIAGNOSTIC_END time=" + Instant.now());
        log.flush();
    }

    // The hook runs inside the test container's PID namespace. Inspect every
    // visible process, including reparented children, without reading command
    // lines, environments, or the targets of non-pipe descriptors into the log.
    private void snapshotPipes() throws InterruptedException {
        StringWriter capture = new StringWriter();
        FutureTask<Void> snapshot = new FutureTask<>(() -> {
            try (PrintWriter output = new PrintWriter(capture)) {
                long parent = ProcessHandle.current().pid();
                int descriptors = snapshotProcess(output, Path.of("/proc", Long.toString(parent)), MAX_DESCRIPTORS);
                int processes = 1;
                try (DirectoryStream<Path> entries = Files.newDirectoryStream(Path.of("/proc"))) {
                    for (Path entry : entries) {
                        String name = entry.getFileName().toString();
                        if (!name.matches("[0-9]+") || name.equals(Long.toString(parent))) {
                            continue;
                        }
                        if (Thread.currentThread().isInterrupted()
                                || processes == MAX_PROCESSES || descriptors == MAX_DESCRIPTORS) {
                            output.println("PROC_SNAPSHOT_TRUNCATED processes=" + processes + " descriptors=" + descriptors);
                            return null;
                        }
                        descriptors += snapshotProcess(output, entry, MAX_DESCRIPTORS - descriptors);
                        processes++;
                    }
                }
                output.println("PROC_SNAPSHOT_COMPLETE processes=" + processes + " descriptors=" + descriptors);
            }
            return null;
        });
        Thread worker = new Thread(snapshot, "timeout-proc-snapshot");
        // A stuck proc read must not prevent jtreg's normal timeout handling.
        worker.setDaemon(true);
        log.println("PROC_SNAPSHOT_BEGIN time=" + Instant.now());
        worker.start();
        try {
            snapshot.get(5, TimeUnit.SECONDS);
        } catch (TimeoutException e) {
            log.println("PROC_SNAPSHOT_TRUNCATED reason=deadline");
        } catch (ExecutionException e) {
            log.println("PROC_SNAPSHOT_ERROR " + e.getCause());
        } finally {
            snapshot.cancel(true);
            // StringWriter retains complete or partial output independently of
            // the daemon; no proc read holds the timeout handler's log lock.
            log.print(capture.toString());
            log.println("PROC_SNAPSHOT_END time=" + Instant.now());
        }
    }

    private static String readProcRecord(Path path) throws IOException {
        try (var input = Files.newInputStream(path)) {
            byte[] bytes = input.readNBytes(MAX_PROC_BYTES + 1);
            if (bytes.length > MAX_PROC_BYTES) {
                throw new IOException("proc record exceeds " + MAX_PROC_BYTES + " bytes: " + path);
            }
            return new String(bytes, StandardCharsets.UTF_8);
        }
    }

    private static int snapshotProcess(PrintWriter output, Path process, int remaining) {
        int descriptors = 0;
        String pid = process.getFileName().toString();
        try {
            for (String line : readProcRecord(process.resolve("status")).split("\n")) {
                if (line.startsWith("Pid:") || line.startsWith("PPid:") || line.startsWith("Tgid:")
                        || line.startsWith("State:") || line.startsWith("Threads:")) {
                    output.println("PROC pid=" + pid + " " + line);
                }
            }
            try (DirectoryStream<Path> entries = Files.newDirectoryStream(process.resolve("fd"))) {
                for (Path descriptor : entries) {
                    if (Thread.currentThread().isInterrupted() || descriptors == remaining) {
                        output.println("PROC_FDS_TRUNCATED pid=" + pid);
                        break;
                    }
                    descriptors++;
                    String fd = descriptor.getFileName().toString();
                    try {
                        String target = Files.readSymbolicLink(descriptor).toString();
                        if (!target.matches("pipe:\\[[0-9]+\\]")) {
                            continue;
                        }
                        String info = readProcRecord(process.resolve("fdinfo").resolve(fd));
                        if (!target.equals(Files.readSymbolicLink(descriptor).toString())) {
                            output.println("PROC_FD_CHANGED pid=" + pid + " fd=" + fd);
                            continue;
                        }
                        // This is an observation, not an atomic ownership
                        // snapshot: PID reuse or same-pipe FD reuse can race it.
                        output.println("PIPE pid=" + pid + " fd=" + fd + " target=" + target);
                        for (String line : info.split("\n")) {
                            if (line.startsWith("flags:")) {
                                output.println("PIPE_FLAGS pid=" + pid + " fd=" + fd + " " + line);
                            }
                        }
                    } catch (IOException e) {
                        output.println("PROC_FD_ERROR pid=" + pid + " fd=" + fd + " " + e);
                    }
                }
            }
        } catch (IOException e) {
            output.println("PROC_ERROR pid=" + pid + " " + e);
        }
        return descriptors;
    }

    /** Verifies capture mechanics separately from jtreg invoking the handler. */
    public static void main(String[] args) throws Exception {
        PrintWriter log = new PrintWriter(System.out, true);
        log.println("TIMEOUT_DIAGNOSTIC_PREFLIGHT_BEGIN");
        File jdk = new File(System.getProperty("java.home"));
        Process child = new ProcessBuilder(new File(jdk, "bin/java").toString(), "-version")
                .inheritIO().start();
        Pipe pipe = Pipe.open();
        try (var reader = pipe.source(); var writer = pipe.sink()) {
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
                    "TimeoutDiagnostic.runActions(", "CHILD alive=false",
                    "CHILD exit_value=0", "CHILD proc_status=absent", "TIMEOUT_DIAGNOSTIC_END",
                    "PROC pid=" + ProcessHandle.current().pid() + " Pid:",
                    "PIPE pid=", "PIPE_FLAGS pid=", "PROC_SNAPSHOT_COMPLETE", "PROC_SNAPSHOT_END"
            }) {
                if (!output.contains(expected)) {
                    throw new AssertionError("preflight missing: " + expected);
                }
            }
            if (output.contains("PROC_SNAPSHOT_TRUNCATED") || output.contains("PROC_FDS_TRUNCATED")
                    || output.contains("PROC_SNAPSHOT_ERROR")) {
                throw new AssertionError("preflight proc snapshot did not complete");
            }
            log.println("TIMEOUT_DIAGNOSTIC_PREFLIGHT_PASS");
        } finally {
            child.destroyForcibly();
            child.waitFor();
        }
    }
}
