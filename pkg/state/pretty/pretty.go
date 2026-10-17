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

// Package pretty is a pretty-printer for state streams.
package pretty

import (
	"fmt"
	"io"
	"strings"

	"gvisor.dev/gvisor/pkg/state"
	"gvisor.dev/gvisor/pkg/state/wire"
)

type printer struct {
	html      bool
	typeSpecs map[string]*wire.Type
}

func (p *printer) formatRef(x *wire.Ref, graph uint64) string {
	baseRef := fmt.Sprintf("g%dr%d", graph, x.Root)
	fullRef := baseRef
	if len(x.Dots) > 0 {
		typ, _ := p.formatType(x.Type, graph)
		var buf strings.Builder
		buf.WriteString("(*")
		buf.WriteString(typ)
		buf.WriteString(")(")
		buf.WriteString(baseRef)
		buf.WriteString(")")
		for _, dot := range x.Dots {
			switch component := dot.(type) {
			case *wire.FieldName:
				buf.WriteString(".")
				buf.WriteString(string(*component))
			case wire.Index:
				fmt.Fprintf(&buf, "[%d]", component)
			default:
				panic(fmt.Sprintf("unreachable: unknown path component %T", component))
			}
		}
		fullRef = buf.String()
	}
	if p.html {
		return fmt.Sprintf("<a href=\"#%s\">%s</a>", baseRef, fullRef)
	}
	return fullRef
}

func (p *printer) formatTypeID(id wire.TypeID, graph uint64) (string, bool) {
	tag := fmt.Sprintf("g%dt%d", graph, id)
	desc := tag
	if spec, ok := p.typeSpecs[tag]; ok {
		desc += fmt.Sprintf("=%s", spec.Name)
	} else {
		desc += "!missing-type-spec"
	}
	if p.html {
		return fmt.Sprintf("<a href=\"#%s\">%s</a>", tag, desc), true
	}
	return desc, true
}

func (p *printer) formatType(t wire.TypeSpec, graph uint64) (string, bool) {
	switch t := t.(type) {
	case wire.TypeID:
		return p.formatTypeID(t, graph)
	case wire.TypeSpecNil:
		return "", false
	case *wire.TypeSpecPointer:
		element, _ := p.formatType(t.Type, graph)
		return fmt.Sprintf("(*%s)", element), true
	case *wire.TypeSpecArray:
		element, _ := p.formatType(t.Type, graph)
		return fmt.Sprintf("[%d](%s)", t.Count, element), true
	case *wire.TypeSpecSlice:
		element, _ := p.formatType(t.Type, graph)
		return fmt.Sprintf("([]%s)", element), true
	case *wire.TypeSpecMap:
		key, _ := p.formatType(t.Key, graph)
		value, _ := p.formatType(t.Value, graph)
		return fmt.Sprintf("(map[%s]%s)", key, value), true
	default:
		panic(fmt.Sprintf("unreachable: unknown type %T", t))
	}
}

// format formats a single object, for pretty-printing. It also returns whether
// the value is a non-zero value.
func (p *printer) format(graph uint64, depth int, encoded wire.Object) (string, bool) {
	switch x := encoded.(type) {
	case wire.Nil:
		return "nil", false
	case wire.NoObjects:
		return "{}", false
	case *wire.String:
		return fmt.Sprintf("%q", *x), *x != ""
	case *wire.Complex64:
		return fmt.Sprintf("%f+%fi", real(*x), imag(*x)), *x != 0
	case *wire.Complex128:
		return fmt.Sprintf("%f+%fi", real(*x), imag(*x)), *x != 0
	case *wire.Ref:
		return p.formatRef(x, graph), x.Root != 0
	case *wire.Type:
		tabs := "\n" + strings.Repeat("\t", depth)
		items := make([]string, 0, len(x.Fields)+2)
		items = append(items, fmt.Sprintf("type %s {", x.Name))
		for i := 0; i < len(x.Fields); i++ {
			items = append(items, fmt.Sprintf("\t%d: %s,", i, x.Fields[i]))
		}
		items = append(items, "}")
		return strings.Join(items, tabs), true // No zero value.
	case *wire.Slice:
		return fmt.Sprintf("%s{len:%d,cap:%d}", p.formatRef(&x.Ref, graph), x.Length, x.Capacity), x.Capacity != 0
	case wire.Array:
		return p.formatArray(graph, depth, x.Len(), x.Element)
	case *wire.MultipleObjects:
		return p.formatArray(graph, depth, len(*x), func(i int) wire.Object { return (*x)[i] })
	case *wire.Struct:
		tag := fmt.Sprintf("g%dt%d", graph, x.TypeID)
		spec := p.typeSpecs[tag]
		typ, _ := p.formatTypeID(x.TypeID, graph)
		if x.Fields() == 0 {
			return fmt.Sprintf("struct[%s]{}", typ), false
		}
		items := make([]string, 0, 2)
		items = append(items, fmt.Sprintf("struct[%s]{", typ))
		tabs := "\n" + strings.Repeat("\t", depth)
		allZero := true
		for i := 0; i < x.Fields(); i++ {
			var name string
			if spec != nil && i < len(spec.Fields) {
				name = spec.Fields[i]
			} else {
				name = fmt.Sprintf("%d", i)
			}
			element, ok := p.format(graph, depth+1, *x.Field(i))
			allZero = allZero && !ok
			items = append(items, fmt.Sprintf("\t%s: %s,", name, element))
		}
		items = append(items, "}")
		return strings.Join(items, tabs), !allZero
	case *wire.Map:
		if len(x.Keys) == 0 {
			return "map{}", false
		}
		items := make([]string, 0, len(x.Keys)+2)
		items = append(items, "map{")
		tabs := "\n" + strings.Repeat("\t", depth)
		for i := range x.Keys {
			key, _ := p.format(graph, depth+1, x.Keys[i])
			value, _ := p.format(graph, depth+1, x.Values[i])
			items = append(items, fmt.Sprintf("\t%s: %s,", key, value))
		}
		items = append(items, "}")
		return strings.Join(items, tabs), true
	case *wire.Interface:
		typ, typOk := p.formatType(x.Type, graph)
		element, elementOk := p.format(graph, depth+1, x.Value)
		return fmt.Sprintf("interface[%s]{%s}", typ, element), typOk || elementOk
	case wire.Bool, wire.Int, wire.Uint, wire.Float32, wire.Float64:
		return fmt.Sprintf("%v", x), true
	default:
		panic(fmt.Sprintf("unreachable: unknown object %T", encoded))
	}
}

func (p *printer) formatArray(graph uint64, depth int, length int, element func(int) wire.Object) (string, bool) {
	if length == 0 {
		return "[]", false
	}
	items := make([]string, 0, length+2)
	zeros := make([]string, 0) // used to eliminate zero entries.
	items = append(items, "[")
	tabs := "\n" + strings.Repeat("\t", depth)
	for i := range length {
		value := element(i)
		item, ok := p.format(graph, depth+1, value)
		if !ok {
			zeros = append(zeros, fmt.Sprintf("\t%s,", item))
			continue
		}
		if len(zeros) > 0 {
			items = append(items, zeros...)
			zeros = nil
		}
		items = append(items, fmt.Sprintf("\t%s,", item))
	}
	if len(zeros) > 0 {
		items = append(items, fmt.Sprintf("\t... (%d zeros),", len(zeros)))
	}
	items = append(items, "]")
	return strings.Join(items, tabs), len(zeros) < length
}

// printStream is the basic print implementation.
func (p *printer) printStream(w io.Writer, r io.Reader) (err error) {
	wr := wire.Reader{Reader: r}

	// current graph ID.
	var graph uint64

	if p.html {
		fmt.Fprintf(w, "<pre>")
		defer fmt.Fprintf(w, "</pre>")
	}

	defer func() {
		if r := recover(); r != nil {
			if rErr, ok := r.(error); ok {
				err = rErr // Override return.
				return
			}
			panic(r) // Propagate.
		}
	}()

	p.typeSpecs = make(map[string]*wire.Type)

	for {
		// Find the first object to begin generation.
		length, object, err := state.ReadHeader(&wr)
		if err == io.EOF {
			// Nothing else to do.
			break
		} else if err != nil {
			return err
		}
		if !object {
			graph++ // Increment the graph.
			if length > 0 {
				fmt.Fprintf(w, "(%d bytes non-object data)\n", length)
				io.Copy(io.Discard, &io.LimitedReader{
					R: r,
					N: int64(length),
				})
			}
			continue
		}

		// Read & unmarshal the object.
		//
		// Note that this loop must match the general structure of the
		// loop in decode.go. But we don't register type information,
		// etc. and just print the raw structures.
		type objectAndID struct {
			id  uint64
			obj wire.Object
		}
		var (
			tid     uint64 = 1
			objects []objectAndID
		)
		for i := uint64(0); i < length; {
			// Unmarshal either a type object or object ID.
			encoded := wire.Load(&wr)
			switch value := encoded.(type) {
			case *wire.Type:
				str, _ := p.format(graph, 0, encoded)
				tag := fmt.Sprintf("g%dt%d", graph, tid)
				p.typeSpecs[tag] = value
				if p.html {
					// See below.
					tag = fmt.Sprintf("<a name=\"%s\">%s</a><a href=\"#%s\">&#9875;</a>", tag, tag, tag)
				}
				if _, err := fmt.Fprintf(w, "%s = %s\n", tag, str); err != nil {
					return err
				}
				tid++
			case wire.Uint:
				// Unmarshal the actual object.
				id := uint64(value)
				objects = append(objects, objectAndID{
					id:  id,
					obj: wire.Load(&wr),
				})
				i++
			default:
				return fmt.Errorf("wanted type or object ID, got %#v", encoded)
			}
		}

		for _, objAndID := range objects {
			// Format the node.
			str, _ := p.format(graph, 0, objAndID.obj)
			tag := fmt.Sprintf("g%dr%d", graph, objAndID.id)
			if p.html {
				// Create a little tag with an anchor next to it for linking.
				tag = fmt.Sprintf("<a name=\"%s\">%s</a><a href=\"#%s\">&#9875;</a>", tag, tag, tag)
			}
			if _, err := fmt.Fprintf(w, "%s = %s\n", tag, str); err != nil {
				return err
			}
		}
	}

	return nil
}

// PrintText reads the stream from r and prints text to w.
func PrintText(w io.Writer, r io.Reader) error {
	return (&printer{}).printStream(w, r)
}

// PrintHTML reads the stream from r and prints html to w.
func PrintHTML(w io.Writer, r io.Reader) error {
	return (&printer{html: true}).printStream(w, r)
}
