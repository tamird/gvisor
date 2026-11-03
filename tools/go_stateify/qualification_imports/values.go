package qualification_imports

import (
	"context"
	v2 "math/rand/v2"
	"time"
)

func (*record) saveRandom() v2.PCG                     { return v2.PCG{} }
func (*record) loadRandom(context.Context, v2.PCG)     {}
func (*record) saveInstant() time.Time                 { return time.Time{} }
func (*record) loadInstant(context.Context, time.Time) {}
