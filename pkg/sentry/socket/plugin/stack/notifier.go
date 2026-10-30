// Copyright 2023 The gVisor Authors.
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

package stack

import (
	"fmt"
	"runtime"
	"sync"
	"syscall"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/atomicbitops"
	"gvisor.dev/gvisor/pkg/sentry/socket/plugin"
	"gvisor.dev/gvisor/pkg/sentry/socket/plugin/cgo"
	"gvisor.dev/gvisor/pkg/waiter"
)

// Notifier holds all the state necessary to issue notifications when
// IO events occur on the observed FDs in plugin stack.
type Notifier struct {
	// epFD is the epoll FD used for event registration. It is initialized by
	// waitAndNotify before signaling ioInit and is immutable afterward.
	epFD int32

	mu sync.Mutex

	// eventMap maps file descriptors to their notification queues
	// and waiting status.
	//
	// +checklocks:mu
	eventMap map[uint32]*plugin.EventInfo
}

const (
	MaxEpollEvents = 128
	SleepInMsecond = 100
)

// NewNotifier initialize the event notifier for plugin stack.
// It will allocate a eventMap with fd as key and corresponding eventInfo
// as value and start a goroutine waiting the arrival of events.
func NewNotifier() *Notifier {
	ioInit := make(chan int32)

	n := &Notifier{
		eventMap: make(map[uint32]*plugin.EventInfo),
	}

	go n.waitAndNotify(ioInit)

	epFD := <-ioInit
	if epFD < 0 {
		return nil
	}
	return n
}

// AddFD adds fd and its event notification state to n.eventMap.
//
// +checklocksexclude:n.mu
func (n *Notifier) AddFD(fd uint32, eventInfo *plugin.EventInfo) {
	n.mu.Lock()
	defer n.mu.Unlock()

	// Panic if we're already notifying on this FD.
	if _, ok := n.eventMap[fd]; ok {
		panic(fmt.Sprintf("File descriptor %d added twice", fd))
	}

	// We have nothing to wait for at the moment. Just add it to the map.
	n.eventMap[fd] = eventInfo
}

// RemoveFD removes fd's event notification state from n.eventMap.
//
// +checklocksexclude:n.mu
func (n *Notifier) RemoveFD(fd uint32) {
	n.mu.Lock()
	defer n.mu.Unlock()
	delete(n.eventMap, fd)
}

// register adds e only if the plugin accepts the resulting subscription.
//
// +checklocksexclude:n.mu
// +checklocksexclude:s.eventInfo.Wq.mu
func (n *Notifier) register(s *socketOperations, e *waiter.Entry) error {
	n.mu.Lock()
	defer n.mu.Unlock()
	q := s.eventInfo.Wq
	q.EventRegister(e)
	if err := n.program(s.fd, &s.eventInfo, q.Events()); err != nil {
		q.EventUnregister(e)
		return err
	}
	return nil
}

// +checklocksexclude:n.mu
// +checklocksexclude:s.eventInfo.Wq.mu
func (n *Notifier) unregister(s *socketOperations, e *waiter.Entry) error {
	n.mu.Lock()
	defer n.mu.Unlock()
	q := s.eventInfo.Wq
	q.EventUnregister(e)
	return n.program(s.fd, &s.eventInfo, q.Events())
}

// +checklocksexclude:n.mu
// +checklocksexclude:s.eventInfo.Wq.mu
func (n *Notifier) updateMask(s *socketOperations, e *waiter.Entry, mask waiter.EventMask) error {
	n.mu.Lock()
	defer n.mu.Unlock()
	info := &s.eventInfo
	old := info.Mask
	var accepted waiter.EventMask
	if err := info.Wq.UpdateHostMask(e, mask, func(aggregate waiter.EventMask) error {
		accepted = aggregate
		return programPluginFD(n.epFD, s.fd, old, aggregate)
	}); err != nil {
		return err
	}
	n.storeMask(info, accepted)
	return nil
}

// waitAndNotify loops waiting for io event notifications from the epoll
// object. Once notifications arrive, they are dispatched to the
// registered queue.
func (n *Notifier) waitAndNotify(ioInit chan int32) error {
	// plugin stack leverages TLS variables, so bind this goroutine with
	// one specific OS thread
	runtime.LockOSThread()

	// If current thread is not the main thread, change the thread name.
	if syscall.Getpid() != syscall.Gettid() {
		threadName := []byte("io-thread\x00")
		if err := unix.Prctl(unix.PR_SET_NAME, uintptr(cgo.GetPtr(threadName)), 0, 0, 0); err != nil {
			return err
		}
	}

	n.epFD = int32(cgo.EpollCreate())

	ioInit <- n.epFD

	var events [MaxEpollEvents]syscall.EpollEvent
	for {
		num := cgo.EpollWait(n.epFD, events[:], MaxEpollEvents, SleepInMsecond)
		if num <= 0 {
			continue
		}

		n.mu.Lock()
		for i := 0; i < num; i++ {
			h := uint32(events[i].Fd)
			eventInfo, ok := n.eventMap[h]
			if !ok {
				continue
			}

			ev := waiter.EventMask(events[i].Events)
			atomicbitops.OrUint64(&eventInfo.Ready, uint64(ev&(eventInfo.Mask|waiter.EventErr|waiter.EventHUp)))
			// When an error occurred, invoke all events
			if ev&(waiter.EventErr|waiter.EventHUp) != 0 {
				ev |= waiter.EventIn | waiter.EventOut
			}
			// Callbacks run under n.mu and the queue's read lock. They must
			// not reenter this notifier or call this queue's methods.
			// They may query socket readiness, which acquires neither lock.
			// checklocks cannot propagate these locks through EventListener.
			eventInfo.Wq.Notify(ev)
		}
		n.mu.Unlock()
	}
}

// program installs the aggregate mask and records it only on success.
//
// +checklocks:n.mu
func (n *Notifier) program(fd uint32, info *plugin.EventInfo, mask waiter.EventMask) error {
	if err := programPluginFD(n.epFD, fd, info.Mask, mask); err != nil {
		return err
	}
	n.storeMask(info, mask)
	return nil
}

// storeMask records the accepted mask while the socket's notifier is locked.
//
// +checklocks:n.mu
func (n *Notifier) storeMask(info *plugin.EventInfo, mask waiter.EventMask) {
	if info.Mask != mask {
		info.Mask = mask
		atomicbitops.AndUint64(&info.Ready, uint64(mask))
	}
}

func programPluginFD(epfd int32, fd uint32, old, mask waiter.EventMask) error {
	if old == mask {
		return nil
	}
	op := syscall.EPOLL_CTL_MOD
	switch {
	case old == 0:
		op = syscall.EPOLL_CTL_ADD
	case mask == 0:
		op = syscall.EPOLL_CTL_DEL
	}
	return cgo.EpollCtl(epfd, op, fd, uint32(mask))
}
