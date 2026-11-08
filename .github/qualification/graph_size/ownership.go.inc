type qualificationZeroOwner struct {
	zero struct{}
	data [128]byte
}

func qualificationKeepZero(es *graphEncodeState) uintptr {
	owner := new(qualificationZeroOwner)
	owner.data[127] = 137
	ptr := unsafe.Pointer(&owner.zero)
	es.ptrTarget(ptr, typeInfoOf(reflect.TypeFor[struct{}]()))
	return uintptr(ptr)
}

func TestQualificationTargetOwnership(t *testing.T) {
	es := newGraphEncodeState(t.Context(), typeInfoOf(reflect.TypeFor[struct{}]()))
	addr := qualificationKeepZero(es)
	if len(es.zeroTargets) != 1 {
		t.Fatalf("got %d deferred zero-sized targets", len(es.zeroTargets))
	}
	// BIND_TARGET is replaced by the matching baseline or candidate accessor.
	BIND_TARGET
	runtime.GC()
	ptr := entry.ptr
	if ptr == nil || uintptr(ptr) != addr || (*qualificationZeroOwner)(ptr).data[127] != 137 {
		t.Fatal("lost the deferred target's live pointee")
	}
	if es.target(id).obj != -1 || !es.scratchAliased(ptr, &typeInfo{inlineStruct: true, size: unsafe.Sizeof(qualificationZeroOwner{})}, 0) {
		t.Fatal("deferred zero-sized pointer not recognized as an alias of this scratch value")
	}
	es.release()
	if entry.ptr != nil || len(es.zeroTargets) != 0 {
		t.Fatal("pooled encoder retained a zero-sized pointee")
	}
	t.Logf("TARGET_LAYOUT %d %d", unsafe.Sizeof(encTarget{}), unsafe.Sizeof(es.zeroTargets[0]))
}
