
func TestQueueMembershipAcrossProcessors(t *testing.T) {
	var ps [2]processor
	var ep Endpoint
	for round := 0; round < 64; round++ {
		start := make(chan struct{})
		result := make(chan int, 2)
		for i := range ps {
			go func(i int) {
				<-start
				if ps[i].epQ.enqueue(&ep) {
					result <- i
				} else {
					result <- -1
				}
			}(i)
		}
		close(start)
		a, b := <-result, <-result
		if (a == -1) == (b == -1) {
			t.Fatalf("round %d: enqueue winners = (%d, %d), want one", round, a, b)
		}
		winner := max(a, b)
		other := 1 - winner
		from, to := &ps[winner].epQ, &ps[other].epQ
		if to.dequeue() != nil {
			t.Fatal("rejected enqueue left a list entry")
		}
		// Give each intrusive list a neighbor whose links must survive the transfer.
		var tail, ahead Endpoint
		if !from.enqueue(&tail) || !to.enqueue(&ahead) {
			t.Fatal("could not initialize neighboring entries")
		}
		// An enqueue on the other processor can race with unlinking.
		// If it loses, the first attempt after the unlink must succeed.
		next := make(chan struct{})
		removed := make(chan *Endpoint, 1)
		claimed := make(chan bool, 1)
		go func() {
			<-next
			removed <- from.dequeue()
		}()
		go func() {
			<-next
			claimed <- to.enqueue(&ep)
		}()
		close(next)
		if <-removed != &ep {
			t.Fatal("source processor removed the wrong endpoint")
		}
		if !<-claimed && !to.enqueue(&ep) {
			t.Fatal("destination could not claim the unlinked endpoint")
		}
		if from.enqueue(&ep) || from.dequeue() != &tail || from.dequeue() != nil {
			t.Fatal("source retained membership or lost its neighboring entry")
		}
		if to.dequeue() != &ahead || to.dequeue() != &ep || to.dequeue() != nil {
			t.Fatal("destination lost or duplicated an entry after transfer")
		}
	}
}

func BenchmarkProcessorQueueMembership(b *testing.B) {
	b.Run("DuplicateEnqueue", func(b *testing.B) {
		var owner epQueue
		var ep Endpoint
		if !owner.enqueue(&ep) {
			b.Fatal("could not claim endpoint")
		}
		b.ReportAllocs()
		b.ResetTimer()
		b.RunParallel(func(pb *testing.PB) {
			var other epQueue
			for pb.Next() {
				if other.enqueue(&ep) {
					b.Error("duplicate enqueue accepted")
					return
				}
			}
		})
	})
	b.Run("DequeueRequeue", func(b *testing.B) {
		var ps [2]processor
		var ep Endpoint
		from, to := &ps[0].epQ, &ps[1].epQ
		if !from.enqueue(&ep) {
			b.Fatal("could not claim endpoint")
		}
		b.ReportAllocs()
		for b.Loop() {
			if from.dequeue() != &ep || !to.enqueue(&ep) {
				b.Fatal("could not transfer endpoint")
			}
			from, to = to, from
		}
	})
}
