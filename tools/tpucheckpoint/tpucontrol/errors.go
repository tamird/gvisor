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

package tpucontrol

import (
	"fmt"
	"strings"

	tpupb "gvisor.dev/gvisor/pkg/sentry/control/tpu_control_proto_go_proto"
)

// actionName returns a short lower-case name for a control action, e.g.
// "checkpoint" for ACTION_CHECKPOINT.
func actionName(a tpupb.ControlAction) string {
	return strings.ToLower(strings.TrimPrefix(a.String(), "ACTION_"))
}

// DiscoveryError indicates failure to find libtpu control threads.
type DiscoveryError struct {
	Err error
	PID int
}

// Error implements error.Error.
func (e *DiscoveryError) Error() string {
	return fmt.Sprintf("discovery for PID %d: %v", e.PID, e.Err)
}

// Unwrap supports errors.Is/As.
func (e *DiscoveryError) Unwrap() error { return e.Err }

// TimeoutError indicates the operation timed out waiting for a response.
type TimeoutError struct {
	Err    error
	TID    int
	Action tpupb.ControlAction
}

// Error implements error.Error.
func (e *TimeoutError) Error() string {
	return fmt.Sprintf("%s timed out on thread %d: %v", actionName(e.Action), e.TID, e.Err)
}

// Unwrap supports errors.Is/As.
func (e *TimeoutError) Unwrap() error { return e.Err }

// ProtocolError indicates a wire-format or response parsing failure.
type ProtocolError struct {
	Err    error
	TID    int
	Action tpupb.ControlAction
}

// Error implements error.Error.
func (e *ProtocolError) Error() string {
	return fmt.Sprintf("%s protocol error on thread %d: %v", actionName(e.Action), e.TID, e.Err)
}

// Unwrap supports errors.Is/As.
func (e *ProtocolError) Unwrap() error { return e.Err }

// ControlFailedError indicates libtpu returned success=false.
type ControlFailedError struct {
	ErrorMessage string
	TID          int
	Action       tpupb.ControlAction
	CurrentState tpupb.RuntimeState
}

// Error implements error.Error.
func (e *ControlFailedError) Error() string {
	if e.ErrorMessage != "" {
		return fmt.Sprintf("%s failed on thread %d: %s", actionName(e.Action), e.TID, e.ErrorMessage)
	}
	return fmt.Sprintf("%s failed on thread %d: state=%s", actionName(e.Action), e.TID, e.CurrentState)
}
