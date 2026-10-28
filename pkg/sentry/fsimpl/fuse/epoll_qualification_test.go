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

package fuse

import (
	"testing"

	"gvisor.dev/gvisor/pkg/abi/linux"
	"gvisor.dev/gvisor/pkg/sentry/vfs"
)

func TestEpollModifyAfterControlledAbort(t *testing.T) {
	s := setup(t)
	conn, file, err := newTestConnection(s, maxActiveRequestsDefault)
	if err != nil {
		t.Fatal(err)
	}
	defer file.DecRef(s.Ctx)
	epfd, err := s.VFS.NewEpollInstanceFD(s.Ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer epfd.DecRef(s.Ctx)
	ep := epfd.Impl().(*vfs.EpollInstance)
	event := linux.EpollEvent{Events: linux.EPOLLOUT | linux.EPOLLONESHOT, Data: [2]int32{1}}
	if err := ep.AddInterest(file, 3, event); err != nil {
		t.Fatal(err)
	}
	if got := ep.ReadEvents(nil, 1); len(got) != 1 || got[0].Events != linux.EPOLLOUT || got[0].Data != event.Data {
		t.Fatalf("connected event: %v", got)
	}
	conn.Abort(s.Ctx)
	event.Data = [2]int32{2}
	if err := ep.ModifyInterest(file, 3, event); err != nil {
		t.Fatalf("same-mask MOD on aborted DeviceFD: %v", err)
	}
	if got := ep.ReadEvents(nil, 1); len(got) != 1 || got[0].Events != linux.EPOLLERR || got[0].Data != event.Data {
		t.Fatalf("aborted event: %v", got)
	}
	if got := ep.ReadEvents(nil, 1); len(got) != 0 {
		t.Fatalf("rearmed one-shot redelivered: %v", got)
	}
}
