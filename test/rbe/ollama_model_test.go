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

package rbe

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"os"
	"os/exec"
	"slices"
	"strings"
	"testing"
	"time"
)

var ollamaModel = flag.String("ollama-model", "", "Model to list and show using the loaded Ollama server")
var ollamaManifest = flag.String("ollama-manifest", "", "Pinned raw manifest for the model runtime check")

// TestOllamaModel checks the actual server's view of the packaged files. With
// networking disabled, a missing model cannot be repaired by a registry pull.
func TestOllamaModel(t *testing.T) {
	image := "gvisor.dev/images/" + *sourceImage + ":latest"
	manifest, err := os.ReadFile(*ollamaManifest)
	if err != nil {
		t.Fatal(err)
	}
	var expected struct {
		Layers []struct {
			MediaType string
			Digest    string
		}
	}
	if err := json.Unmarshal(manifest, &expected); err != nil {
		t.Fatal(err)
	}
	var modelDigest string
	for _, layer := range expected.Layers {
		if layer.MediaType == "application/vnd.ollama.image.model" {
			if modelDigest != "" {
				t.Fatal("expected exactly one model layer")
			}
			modelDigest = layer.Digest
		}
	}
	if modelDigest == "" {
		t.Fatal("manifest contains no model layer")
	}
	output, err := exec.CommandContext(t.Context(), "docker", "run", "--detach", "--network=none", image).CombinedOutput()
	if err != nil {
		t.Fatalf("start packaged Ollama server: %v\n%s", err, output)
	}
	container := strings.TrimSpace(string(output))
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.WithoutCancel(t.Context()), time.Minute)
		defer cancel()
		output, err := exec.CommandContext(ctx, "docker", "logs", container).CombinedOutput()
		t.Logf("Ollama server logs (error=%v):\n%s", err, output)
		if output, err := exec.CommandContext(ctx, "docker", "rm", "--force", container).CombinedOutput(); err != nil {
			t.Errorf("remove Ollama server: %v\n%s", err, output)
		}
	})
	ctx, cancel := context.WithTimeout(t.Context(), time.Minute)
	defer cancel()
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	for {
		output, err = exec.CommandContext(ctx, "docker", "exec", container, "ollama", "list").CombinedOutput()
		if err == nil {
			break
		}
		select {
		case <-ctx.Done():
			t.Fatalf("list packaged model: %v\n%s", err, output)
		case <-tick.C:
		}
	}
	t.Logf("ollama list:\n%s", output)
	rows := strings.Split(strings.TrimSpace(string(output)), "\n")
	if got, want := len(rows), 2; got != want {
		t.Fatalf("model list has %d lines, want %d: %s", got, want, output)
	}
	fields := strings.Fields(rows[1])
	if len(fields) < 2 {
		t.Fatalf("model list row has no name and digest: %q", rows[1])
	}
	if got, want := fields[0], *ollamaModel; got != want {
		t.Errorf("listed model = %q, want %q", got, want)
	}
	manifestHash := sha256.Sum256(manifest)
	if got, want := fields[1], hex.EncodeToString(manifestHash[:])[:12]; got != want {
		t.Errorf("listed manifest digest = %q, want %q", got, want)
	}
	output, err = exec.CommandContext(t.Context(), "docker", "exec", container, "ollama", "show", "--modelfile", *ollamaModel).CombinedOutput()
	if err != nil {
		t.Fatalf("show packaged model: %v\n%s", err, output)
	}
	t.Logf("ollama show --modelfile:\n%s", output)
	want := "FROM /root/.ollama/models/blobs/" + strings.ReplaceAll(modelDigest, ":", "-")
	if !slices.Contains(strings.Split(string(output), "\n"), want) {
		t.Errorf("model definition does not contain %q:\n%s", want, output)
	}
}
