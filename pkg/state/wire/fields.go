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

package wire

// Generated scalar captures snapshot values into the same owned Object slots
// as dynamic capture. Ordinary zero fields are elided; values nested inside
// interfaces and arrays retain their complete representation.

// CaptureIntField snapshots a scalar field with zero elision.
func CaptureIntField(dest *Object, value Int) {
	if value == 0 {
		*dest = Nil{}
	} else {
		*dest = value
	}
}

// CaptureStringField snapshots a scalar field with zero elision.
func CaptureStringField(dest *Object, value String) {
	if value == "" {
		*dest = Nil{}
	} else {
		*dest = &value
	}
}

// CaptureUintField snapshots a scalar field with zero elision.
func CaptureUintField(dest *Object, value Uint) {
	if value == 0 {
		*dest = Nil{}
	} else {
		*dest = value
	}
}

// CaptureBoolField snapshots a scalar field with zero elision.
func CaptureBoolField(dest *Object, value Bool) {
	if value == false {
		*dest = Nil{}
	} else {
		*dest = value
	}
}

// CaptureFloat32Field preserves the promotion and narrowing boundary used
// by reflective capture, including signaling NaNs.
func CaptureFloat32Field(dest *Object, value Float64) {
	if value == 0 {
		*dest = Nil{}
	} else {
		*dest = Float32(value)
	}
}

// CaptureFloat64Field snapshots a scalar field with zero elision.
func CaptureFloat64Field(dest *Object, value Float64) {
	if value == 0 {
		*dest = Nil{}
	} else {
		*dest = value
	}
}

// CaptureComplex64Field preserves the promotion and narrowing boundary used
// by reflective capture, including signaling NaNs.
func CaptureComplex64Field(dest *Object, value Complex128) {
	if value == 0 {
		*dest = Nil{}
	} else {
		narrow := Complex64(value)
		*dest = &narrow
	}
}

// CaptureComplex128Field snapshots a scalar field with zero elision.
func CaptureComplex128Field(dest *Object, value Complex128) {
	if value == 0 {
		*dest = Nil{}
	} else {
		*dest = &value
	}
}
