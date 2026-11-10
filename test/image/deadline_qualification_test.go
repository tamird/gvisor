package image

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"runtime"
	"strings"
	"testing"
	"time"

	"gvisor.dev/gvisor/pkg/test/dockerutil"
	"gvisor.dev/gvisor/pkg/test/testutil"
)

// savedDockerExec is copied verbatim from the upstream parent, apart from its name.
func savedDockerExec(ctx context.Context, d *dockerutil.Container, cmd []string) (string, error) {
	execProc, err := d.ExecProcess(ctx, dockerutil.ExecOpts{}, cmd...)
	if err != nil {
		return "", fmt.Errorf("docker exec failed: %v", err)
	}
	output, err := execProc.Logs()
	if err != nil {
		return "", fmt.Errorf("docker logs failed: %v", err)
	}
	status, err := execProc.WaitExitStatus(ctx)
	if err != nil {
		return "", fmt.Errorf("failed to get exit code: %v", err)
	}
	if status != 0 {
		return output, fmt.Errorf("command %v failed with status %d, output: %q", cmd, status, output)
	}
	return output, nil
}

func TestQualificationStalledPeer(t *testing.T) {
	for _, tc := range []struct {
		name string
		call func(context.Context, *dockerutil.Container, []string) (string, error)
	}{
		{"old", savedDockerExec},
		{"revised", dockerInGvisorExecOutput},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx, stop := context.WithTimeout(t.Context(), 12*time.Second)
			defer stop()
			attached := make(chan net.Conn, 1)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch {
				case strings.HasSuffix(r.URL.Path, "/_ping"):
					w.Header().Set("API-Version", "1.43")
					fmt.Fprint(w, "OK")
				case strings.HasSuffix(r.URL.Path, "/containers/create"):
					w.Header().Set("Content-Type", "application/json")
					w.WriteHeader(http.StatusCreated)
					fmt.Fprint(w, `{"Id":"fixture-container"}`)
				case strings.HasSuffix(r.URL.Path, "/fixture-container/exec"):
					w.Header().Set("Content-Type", "application/json")
					w.WriteHeader(http.StatusCreated)
					fmt.Fprint(w, `{"Id":"fixture-exec"}`)
				case strings.HasSuffix(r.URL.Path, "/fixture-exec/start"):
					c, b, err := w.(http.Hijacker).Hijack()
					if err != nil {
						t.Error(err)
						return
					}
					fmt.Fprint(b, "HTTP/1.1 101 UPGRADED\r\nContent-Type: application/vnd.docker.raw-stream\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n")
					if err := b.Flush(); err != nil {
						c.Close()
						t.Error(err)
						return
					}
					attached <- c
				case strings.HasSuffix(r.URL.Path, "/fixture-exec/json"):
					// Withhold replies as well as leaving the attached output stream open.
					select {
					case <-r.Context().Done():
					case <-ctx.Done():
					}
				default:
					http.NotFound(w, r)
					t.Errorf("unexpected Docker RPC %s %s", r.Method, r.URL.Path)
				}
			}))
			defer server.Close()
			t.Setenv("DOCKER_API_VERSION", "1.43")
			t.Setenv("DOCKER_HOST", "tcp://"+strings.TrimPrefix(server.URL, "http://"))
			d := dockerutil.MakeNativeContainer(ctx, t)
			if err := d.Create(ctx, dockerutil.RunOpts{Image: "basic/alpine"}, "true"); err != nil {
				t.Fatal(err)
			}
			execCtx, cancel := context.WithCancel(ctx)
			defer cancel()
			done := make(chan error, 1)
			go func() { _, err := tc.call(execCtx, d, []string{"true"}); done <- err }()
			var peer net.Conn
			select {
			case peer = <-attached:
			case early := <-done:
				t.Fatalf("exec returned before attach: %v", early)
			case <-ctx.Done():
				t.Fatal(ctx.Err())
			}
			defer peer.Close()
			if err := testutil.PollContext(ctx, func() error {
				buf := make([]byte, 256<<10)
				stacks := string(buf[:runtime.Stack(buf, true)])
				for _, stack := range strings.Split(stacks, "\n\n") {
					if strings.Contains(stack, "stdcopy.StdCopy") && strings.Contains(stack, "(*Process).Logs") {
						return nil
					}
				}
				return errors.New("exec output reader has not blocked")
			}); err != nil {
				t.Fatal(err)
			}
			cancel()
			select {
			case err := <-done:
				if tc.name == "old" || !errors.Is(err, context.Canceled) {
					t.Fatalf("early return: %v", err)
				}
				t.Log("Revised exec returned context.Canceled while peer still held the stream open and withheld inspect replies")
			case <-time.After(2 * time.Second):
				if tc.name != "old" {
					t.Fatal("revised caller remained blocked in output read")
				}
				t.Log("Stock exec ignored cancellation with the stream open")
				peer.Close()
				select {
				case err := <-done:
					if err == nil {
						t.Fatal("stock exec reported success")
					}
				case <-ctx.Done():
					t.Fatal("stock reader failed to join after peer close")
				}
			}
		})
	}
}

func TestQualificationDockerStreams(t *testing.T) {
	ctx := t.Context()
	d := dockerutil.MakeNativeContainer(ctx, t)
	defer d.CleanUp(ctx)
	if err := d.Spawn(ctx, dockerutil.RunOpts{Image: "basic/alpine"}, "sleep", "120"); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name string
		call func(context.Context, *dockerutil.Container, []string) (string, error)
	}{{"old", savedDockerExec}, {"revised", dockerInGvisorExecOutput}} {
		t.Run(tc.name, func(t *testing.T) {
			for _, code := range []int{0, 23} {
				cmd := []string{"sh", "-c", fmt.Sprintf("printf stdout; printf stderr >&2; exit %d", code)}
				out, err := tc.call(ctx, d, cmd)
				if !strings.Contains(out, "stdout") || !strings.Contains(out, "stderr") || (err != nil) != (code != 0) {
					t.Fatalf("exit %d output=%q err=%v", code, out, err)
				}
				if code != 0 && (!strings.Contains(err.Error(), "23") || !strings.Contains(err.Error(), out)) {
					t.Fatalf("missing exit/output diagnostic: %v", err)
				}
				t.Logf("exit %d output %q error %v", code, out, err)
			}
		})
	}
}
