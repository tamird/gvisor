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

package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"slices"
	"strconv"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/sync"
	"gvisor.dev/gvisor/pkg/tcpip"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
	"gvisor.dev/gvisor/pkg/tcpip/transport/tcp"
)

const (
	tcpObservationInterval = 100 * time.Millisecond
	tcpObservationLimit    = 12000
)

// tcpObservation is the benchmark's projection of an owned endpoint snapshot.
// Addresses and stack timestamps have private representations. Default gob
// encoding rejects those structs, while JSON would omit their private fields.
type tcpObservation struct {
	Local       string
	Remote      string
	BootBeginNS int64
	BootEndNS   int64
	UnixNS      int64
	Netstack    *tcpNetstackObservation `json:",omitempty"`
	Native      *tcpNativeObservation   `json:",omitempty"`
}

type tcpNetstackObservation struct {
	StackTimeNS          string
	Cwnd                 int
	Ssthresh             int
	Outstanding          int
	SackedOut            int
	SendWindowBytes      uint32
	SndUna               uint32
	SndNxt               uint32
	MSS                  int
	SendBufferUsed       int
	SendBufferSize       int
	ReceiveBufferUsed    int
	RTT                  tcp.TCPRTTState
	RTO                  time.Duration
	DupACKs              int
	Recovery             tcp.TCPFastRecoveryState
	RACKRTT              time.Duration
	RACKReorderingWindow time.Duration
	SpuriousRecovery     bool
	CubicWMax            float64
	CubicWEst            float64
	CubicEpochAge        time.Duration
	SACKBlocks           int
	ReceivedSACKBlocks   int
}

// tcpNativeObservation records only fields returned by TCP_INFO. Durations
// use nanoseconds, matching Netstack; unsupported tail fields remain nil.
type tcpNativeObservation struct {
	InfoBytes            uint32
	CongestionControl    string
	State                uint8
	CongestionState      uint8
	Cwnd                 uint32
	Ssthresh             uint32
	MSS                  uint32
	RTT                  time.Duration
	RTTVar               time.Duration
	RTO                  time.Duration
	Unacked              uint32
	Sacked               uint32
	Lost                 uint32
	Retrans              uint32
	TotalRetrans         uint32
	NotSentBytes         *uint32
	BytesAcked           *uint64
	DeliveryRate         *uint64
	BusyTime             *time.Duration
	ReceiveWindowLimited *time.Duration
	SendBufferLimited    *time.Duration
}

type nativeTCPConnection struct {
	control syscall.RawConn
	local   string
	remote  string
}

// tcpRecorder owns capture memory and its output. Packet callbacks only append
// bounded records; file I/O happens after the sampler is joined at shutdown.
type tcpRecorder struct {
	file *os.File
	mode string
	role string
	stop chan struct{}
	done chan struct{}
	mu   sync.Mutex
	// +checklocks:mu
	records []tcpObservation
	// +checklocks:mu
	nativeConnections []nativeTCPConnection
	// +checklocks:mu
	unavailable int
	// +checklocks:mu
	truncated bool
	// +checklocks:mu
	closed bool
	// +checklocks:mu
	err error
}

func newTCPRecorder(path, mode, role string) (*tcpRecorder, error) {
	if path == "" {
		return nil, nil
	}
	file, err := os.Create(path)
	if err != nil {
		return nil, fmt.Errorf("create TCP observations: %w", err)
	}
	return &tcpRecorder{file: file, mode: mode, role: role}, nil
}

func bootTimeNS() (int64, error) {
	var ts unix.Timespec
	if err := unix.ClockGettime(unix.CLOCK_BOOTTIME, &ts); err != nil {
		return 0, err
	}
	return ts.Nano(), nil
}

func (r *tcpRecorder) fail(err error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if !r.closed && r.err == nil {
		r.err = err
	}
}

func (r *tcpRecorder) append(record tcpObservation) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		return
	}
	if len(r.records) == tcpObservationLimit {
		r.truncated = true
		return
	}
	r.records = append(r.records, record)
}

func (r *tcpRecorder) record(state *tcp.TCPEndpointState, begin, end int64) {
	s := state.Sender
	r.append(tcpObservation{
		Local:       net.JoinHostPort(state.ID.LocalAddress.String(), strconv.Itoa(int(state.ID.LocalPort))),
		Remote:      net.JoinHostPort(state.ID.RemoteAddress.String(), strconv.Itoa(int(state.ID.RemotePort))),
		BootBeginNS: begin,
		BootEndNS:   end,
		UnixNS:      time.Now().UnixNano(),
		Netstack: &tcpNetstackObservation{
			StackTimeNS:          state.SegTime.String(),
			Cwnd:                 s.SndCwnd,
			Ssthresh:             s.Ssthresh,
			Outstanding:          s.Outstanding,
			SackedOut:            s.SackedOut,
			SendWindowBytes:      uint32(s.SndWnd),
			SndUna:               uint32(s.SndUna),
			SndNxt:               uint32(s.SndNxt),
			MSS:                  s.MaxPayloadSize,
			SendBufferUsed:       state.SndBufState.SndBufUsed,
			SendBufferSize:       state.SndBufState.SndBufSize,
			ReceiveBufferUsed:    state.RcvBufState.RcvBufUsed,
			RTT:                  s.RTTState,
			RTO:                  s.RTO,
			DupACKs:              s.DupAckCount,
			Recovery:             s.FastRecovery,
			RACKRTT:              s.RACKState.RTT,
			RACKReorderingWindow: s.RACKState.ReoWnd,
			SpuriousRecovery:     s.SpuriousRecovery,
			CubicWMax:            s.Cubic.WMax,
			CubicWEst:            s.Cubic.WEst,
			CubicEpochAge:        s.Cubic.TimeSinceLastCongestion,
			SACKBlocks:           len(state.SACK.Blocks),
			ReceivedSACKBlocks:   len(state.SACK.ReceivedBlocks),
		},
	})
}

func (r *tcpRecorder) recordPacket(state *tcp.TCPEndpointState) {
	now, err := bootTimeNS()
	if err != nil {
		r.fail(err)
		return
	}
	// A callback timestamp is not a bracket around the earlier state copy.
	r.record(state, now, now)
}

func (r *tcpRecorder) start(sample func() error) {
	r.stop = make(chan struct{})
	r.done = make(chan struct{})
	go func() {
		defer close(r.done)
		ticker := time.NewTicker(tcpObservationInterval)
		defer ticker.Stop()
		for {
			select {
			case <-r.stop:
				return
			case <-ticker.C:
			}
			if err := sample(); err != nil {
				r.fail(err)
				return
			}
		}
	}()
}

func (r *tcpRecorder) unavailableEndpoint() {
	r.mu.Lock()
	r.unavailable++
	r.mu.Unlock()
}

func (r *tcpRecorder) sampleNetstack(s *stack.Stack) error {
	seen := make(map[*tcp.Endpoint]struct{})
	for _, registered := range s.RegisteredEndpoints() {
		ep, ok := registered.(*tcp.Endpoint)
		if !ok {
			continue
		}
		if _, ok := seen[ep]; ok {
			continue
		}
		seen[ep] = struct{}{}
		begin, err := bootTimeNS()
		if err != nil {
			return err
		}
		state, snapshotErr := ep.StateSnapshot()
		end, err := bootTimeNS()
		if err != nil {
			return err
		}
		if snapshotErr != nil {
			if _, ok := snapshotErr.(*tcpip.ErrNotConnected); !ok {
				return fmt.Errorf("TCP snapshot: %s", snapshotErr)
			}
			r.unavailableEndpoint()
			continue
		}
		r.record(state, begin, end)
	}
	return nil
}

// trackNative runs once per connection, outside the packet path. RawConn keeps
// the descriptor valid during Control even when the application calls Close.
func (r *tcpRecorder) trackNative(conn net.Conn) {
	if r == nil {
		return
	}
	tcpConn, ok := conn.(*net.TCPConn)
	if !ok {
		r.fail(fmt.Errorf("native TCP observation: unexpected connection %T", conn))
		return
	}
	control, err := tcpConn.SyscallConn()
	if err != nil {
		r.fail(err)
		return
	}
	entry := nativeTCPConnection{control: control, local: conn.LocalAddr().String(), remote: conn.RemoteAddr().String()}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		return
	}
	if len(r.nativeConnections) == tcpObservationLimit {
		r.truncated = true
		return
	}
	r.nativeConnections = append(r.nativeConnections, entry)
}

func (r *tcpRecorder) sampleNative() error {
	r.mu.Lock()
	connections := slices.Clone(r.nativeConnections)
	r.mu.Unlock()
	for _, conn := range connections {
		begin, err := bootTimeNS()
		if err != nil {
			return err
		}
		var native tcpNativeObservation
		var captureErr error
		err = conn.control.Control(func(fd uintptr) {
			native, captureErr = readNativeTCPInfo(fd)
		})
		end, clockErr := bootTimeNS()
		if clockErr != nil {
			return clockErr
		}
		if errors.Is(err, net.ErrClosed) {
			r.unavailableEndpoint()
			continue
		}
		if err != nil || captureErr != nil {
			return errors.Join(err, captureErr)
		}
		r.append(tcpObservation{
			Local:       conn.local,
			Remote:      conn.remote,
			BootBeginNS: begin,
			BootEndNS:   end,
			UnixNS:      time.Now().UnixNano(),
			Native:      &native,
		})
	}
	return nil
}

func (r *tcpRecorder) close() error {
	if r == nil {
		return nil
	}
	if r.stop != nil {
		close(r.stop)
		<-r.done
	}
	r.mu.Lock()
	r.closed = true
	captureErr := r.err
	if r.truncated {
		captureErr = errors.Join(captureErr, fmt.Errorf("TCP observation limit %d reached", tcpObservationLimit))
	}
	report := struct {
		Mode                        string
		Role                        string
		ConfiguredCongestionControl string
		IntervalNS                  int64
		Limit                       int
		Unavailable                 int
		Truncated                   bool
		Error                       string
		Records                     []tcpObservation
	}{
		Mode:                        r.mode,
		Role:                        r.role,
		ConfiguredCongestionControl: *congestionControl,
		Limit:                       tcpObservationLimit,
		Unavailable:                 r.unavailable,
		Truncated:                   r.truncated,
		Records:                     r.records,
	}
	// No callback can append after closed is set. Encode outside the mutex so
	// a late packet callback never waits for file I/O while holding e.mu.
	r.mu.Unlock()
	if r.mode == "periodic" {
		report.IntervalNS = int64(tcpObservationInterval)
	}
	if captureErr != nil {
		report.Error = captureErr.Error()
	}
	encodeErr := json.NewEncoder(r.file).Encode(report)
	return errors.Join(captureErr, encodeErr, r.file.Close())
}
