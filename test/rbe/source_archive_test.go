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
	"encoding/json"
	"os/exec"
	"slices"
	"testing"
)

func TestSourceArchiveLoad(t *testing.T) {
	image := verifySourceArchive(t)
	args := append([]string{"image", "inspect"}, image.tags...)
	output, err := exec.CommandContext(t.Context(), "docker", args...).CombinedOutput()
	if err != nil {
		t.Fatalf("inspect loaded aliases: %v\n%s", err, output)
	}
	var loaded []struct {
		ID       string `json:"Id"`
		RepoTags []string
		Size     int64
		RootFS   struct{ Layers []string }
	}
	if err := json.Unmarshal(output, &loaded); err != nil {
		t.Fatal(err)
	}
	if got, want := len(loaded), 2; got != want {
		t.Fatalf("loaded images = %d, want %d", got, want)
	}
	for i, actual := range loaded {
		t.Logf("Loaded %s: expanded image size=%d bytes", image.tags[i], actual.Size)
		if got, want := actual.ID, image.configID; got != want {
			t.Errorf("tag %q config = %q, want %q", image.tags[i], got, want)
		}
		if !slices.Equal(actual.RootFS.Layers, image.diffIDs) {
			t.Errorf("tag %q layers = %q, want %q", image.tags[i], actual.RootFS.Layers, image.diffIDs)
		}
		for _, tag := range image.tags {
			if !slices.Contains(actual.RepoTags, tag) {
				t.Errorf("loaded tags = %q, want %q present", actual.RepoTags, tag)
			}
		}
	}
}
