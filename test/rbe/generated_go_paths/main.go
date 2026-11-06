package main

import (
	"encoding/json"
	"os"
	"reflect"
	"runtime"

	"gvisor.dev/gvisor/pkg/atomicbitops"
)

type frame struct {
	Function string
	File     string
	Line     int
}

func source(method any) frame {
	pc := reflect.ValueOf(method).Pointer()
	function := runtime.FuncForPC(pc)
	file, line := function.FileLine(pc)
	return frame{function.Name(), file, line}
}

func main() {
	if err := json.NewEncoder(os.Stdout).Encode(map[string]frame{
		"checked_in": source((*atomicbitops.Int32).Load),
		"generated":  source((*atomicbitops.Int32).StateTypeName),
	}); err != nil {
		panic(err)
	}
}
