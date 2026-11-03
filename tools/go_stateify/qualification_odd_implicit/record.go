package qualification_odd_implicit

import ("context"; "gvisor.dev/gvisor/tools/go_stateify/qualification_odddep")

// +stateify savable
type record struct { field int `state:".(shapes.Value)"` }
func (*record) saveField() shapes.Value { return 42 }
func (*record) loadField(context.Context, shapes.Value) {}
