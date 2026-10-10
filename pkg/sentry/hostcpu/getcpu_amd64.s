// Copyright 2018 The gVisor Authors.
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

#include "textflag.h"

// func GetCPU() uint32
TEXT ·GetCPU(SB),NOSPLIT|NOFRAME,$0-4
	BYTE $0x0f; BYTE $0x01; BYTE $0xf9; // RDTSCP
	// On Linux, the bottom 12 bits of IA32_TSC_AUX are CPU and the upper 20
	// are node. See arch/x86/entry/vdso/vma.c:vgetcpu_cpu_init().
	ANDL	$0xfff, CX
	MOVL	CX, ret+0(FP)
	RET

// func ClockSample() (cycles uint64, aux uint64, available bool)
TEXT ·ClockSample(SB),NOSPLIT|NOFRAME,$0-17
	BYTE $0x0f; BYTE $0x01; BYTE $0xf9; // RDTSCP
	LFENCE
	SHLQ	$32, DX
	ORQ	DX, AX
	MOVQ	AX, cycles+0(FP)
	MOVQ	CX, aux+8(FP)
	MOVB	$1, available+16(FP)
	RET
