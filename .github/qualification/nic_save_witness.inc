
// Fork-only witness for the beforeSave hook's cross-NIC drain order.
type qualificationSaveLink struct {
	fwdTestLinkEndpoint
	detach func()
	close func()
}
func (e *qualificationSaveLink) Attach(d NetworkDispatcher) {
	if d == nil { e.detach() }
	e.fwdTestLinkEndpoint.Attach(d)
}
func (e *qualificationSaveLink) Close() { e.close() }

func TestQualificationBeforeSaveClosesAfterDetach(t *testing.T) {
	s := New(Options{})
	defer s.Close()
	const count = 2
	detaches, closes := 0,0
	for i := range count {
		e := &qualificationSaveLink{
			fwdTestLinkEndpoint: fwdTestLinkEndpoint{mtu: 1500},
			detach: func(){ detaches++ },
			close: func(){
				closes++
				if detaches != count {t.Errorf("beforeSave closed a link after %d of %d detaches",detaches,count)}
			},
		}
		if err := s.CreateNIC(tcpip.NICID(i+1), e); err != nil {t.Fatal(err)}
	}
	s.SetRemoveConf(true)
	s.beforeSave()
	if detaches != count || closes!=count {t.Fatalf("detaches=%d closes=%d",detaches,closes)}
}
