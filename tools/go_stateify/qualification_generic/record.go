package qualification_generic

import ("context"; clock "time")

type box[T any] struct{ Data T }

// +stateify savable
type record struct { field int `state:".(map[clock.Duration][]box[clock.Time])"` }
func (*record) saveField() map[clock.Duration][]box[clock.Time] { return nil }
func (*record) loadField(context.Context, map[clock.Duration][]box[clock.Time]) {}
