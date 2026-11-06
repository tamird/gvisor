//go:build tamago

package tcp

import (
	"gvisor.dev/gvisor/pkg/sync"
	"gvisor.dev/gvisor/pkg/tcpip/header"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
)

// ObserveProcessorProgress queues one reset on each of two listeners. The
// goroutine holding the first listener's lock can unlock only after the
// processor consumes the second listener's packet and lets the holder run.
func ObserveProcessorProgress(hold bool) {
	st := stack.New(stack.Options{})
	defer st.Destroy()
	var p processor
	p.sleeper.AddWaker(&p.newEndpointWaker)
	p.sleeper.AddWaker(&p.closeWaker)
	p.sleeper.AddWaker(&p.pauseWaker)

	mode := "control"
	if hold {
		mode = "contended"
	}
	queue := func(name string) (*Endpoint, <-chan struct{}) {
		done := make(chan struct{})
		ep := &Endpoint{stack: st}
		ep.segmentQueue.ep = ep
		ep.state.Store(uint32(StateListen))
		s := newSegment()
		s.flags = header.TCPFlagRst
		s.pkt = stack.NewPacketBuffer(stack.PacketBufferOptions{OnRelease: func() {
			println("DISPATCH", mode, name)
			close(done)
		}})
		if !ep.enqueueSegment(s) {
			panic("empty listener queue rejected a reset")
		}
		s.DecRef()
		p.epQ.enqueue(ep)
		return ep, done
	}

	holder, first := queue("FIRST")
	_, peer := queue("PEER")
	if hold {
		holder.StopWork()
	}
	var wg sync.WaitGroup
	wg.Add(1)
	p.newEndpointWaker.Assert()
	go p.start(&wg)
	<-peer
	println("DISPATCH", mode, "OWNER")
	if hold {
		holder.ResumeWork()
	}
	<-first
	p.closeWaker.Assert()
	wg.Wait()
}
