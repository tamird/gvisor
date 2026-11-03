package qualification_literal

import ("context"; "gvisor.dev/gvisor/tools/go_stateify/qualification_major/v2")

// +stateify savable
type record struct { field int `state:".(v2.Value)"` }
func (*record) saveField() v2.Value { return 7 }
func (*record) loadField(context.Context, v2.Value) {}
