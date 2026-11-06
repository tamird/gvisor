
func TestWaiterStorage(t *testing.T) {
	ctx := contexttest.Context(t)
	s := &Set{obj: &ipc.Object{ID: 123}, sems: make([]sem, 2)}
	block := func(num uint16, delta int16) (chan struct{}, int32) {
		s.mu.Lock()
		ch,index,err:=s.executeOps(ctx, []linux.Sembuf{{SemNum:num,SemOp:delta}},42)
		s.mu.Unlock()
		if err!=nil || ch==nil {t.Fatalf("blocking operation = %v %v",ch,err)}
		return ch,index
	}
	allocs:=testing.AllocsPerRun(200,func() {
		ch,index:=block(1,-1)
		s.AbortWait(index,ch)
	})
	fmt.Printf("SEM_LAYOUT member=%d set=%d cycle_allocs=%.1f\n",unsafe.Sizeof(sem{}),unsafe.Sizeof(Set{}),allocs)
	initial := state.GraphEngineEnabled()
	defer state.EnableGraphEngine(initial)
	for _,graph := range []bool{false,true}{
		state.EnableGraphEngine(graph)
		ch0,i0:=block(0,-1)
		// Reset the value from the preceding engine's roundtrip.
		s.mu.Lock()
		s.sems[1].value=0
		s.mu.Unlock()
		ch1,i1:=block(1,-2)
		ch2,_:=block(1,-1)
		var buf bytes.Buffer
		_,activeErr:=state.Save(ctx,&buf,s)
		s.AbortWait(i0,ch0)
		s.mu.Lock()
		ch,index,err:=s.executeOps(ctx,[]linux.Sembuf{{SemNum:1,SemOp:1}},42)
		s.mu.Unlock()
		if ch!=nil || err!=nil || index!=0 || !signalled(ch2) || signalled(ch1) || signalled(ch0) { t.Fatal("mixed queue wake and cancellation") }
		s.AbortWait(i1,ch1)
		buf.Reset()
		if _,err:=state.Save(ctx,&buf,s); err!=nil {t.Fatal("saving empty queues:",err)}
		id:="WIRE";if graph{id="GRAPH"}
		encoded:=buf.Bytes()
		input:=encoded
		if prior:=os.Getenv("SEM_OLD_"+id);prior!="" {
			var e error
			input,e=base64.StdEncoding.DecodeString(prior)
			if e!=nil{t.Fatal(e)}
		}
		var out Set
		if _,err:=state.Load(ctx,bytes.NewReader(input),&out);err!=nil{t.Fatal("roundtrip:",err)}
		value:=out.sems[1].value
		pid:=out.sems[1].pid
		want:=int16(1)
		if value!=want || pid!=42 || out.Size()!=2 {t.Fatalf("restored %d semaphores, value=%d pid=%d",out.Size(),value,pid)}
		fmt.Printf("SEM_STATE %s active_rejected=%t old=%t count=%d value=%d pid=%d stream=%s\n",id,activeErr!=nil && strings.Contains(strings.ToLower(activeErr.Error()),"waiter"),os.Getenv("SEM_OLD_"+id)!="",out.Size(),value,pid,base64.StdEncoding.EncodeToString(encoded))
	}
}
