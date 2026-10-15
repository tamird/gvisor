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

func (p *printer) formatRef(x wire.ReferenceView, graph uint64) string {
	baseRef := fmt.Sprintf("g%dr%d", graph, x.Root())
	fullRef := baseRef
	if x.Dots() > 0 {
		typ, _ := p.formatType(x.Type(), graph)
		var buf strings.Builder
		buf.WriteString("(*")
		buf.WriteString(typ)
		buf.WriteString(")(")
		buf.WriteString(baseRef)
		buf.WriteString(")")
		for i := 0; i < x.Dots(); i++ {
			component := x.Dot(i)
			if component.Field {
				buf.WriteString(".")
				buf.WriteString(component.Name)
			} else {
				fmt.Fprintf(&buf, "[%d]", component.Index)
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

func (p *printer) formatType(t wire.TypeView, graph uint64) (string, bool) {
	switch t.Kind() {
	case wire.TypeIDKind:
		return p.formatTypeID(t.ID(), graph)
	case wire.TypeNilKind:
		return "", false
	case wire.TypePointerKind:
		element, _ := p.formatType(t.Element(), graph)
		return fmt.Sprintf("(*%s)", element), true
	case wire.TypeArrayKind:
		element, _ := p.formatType(t.Element(), graph)
		return fmt.Sprintf("[%d](%s)", t.Count(), element), true
	case wire.TypeSliceKind:
		element, _ := p.formatType(t.Element(), graph)
		return fmt.Sprintf("([]%s)", element), true
	case wire.TypeMapKind:
		keyType, valueType := t.Map()
		key, _ := p.formatType(keyType, graph)
		value, _ := p.formatType(valueType, graph)
		return fmt.Sprintf("(map[%s]%s)", key, value), true
	default:
		panic(fmt.Sprintf("unreachable: unknown type %v", t.Kind()))
	}
}

// format formats a single object, for pretty-printing. It also returns whether
// the value is a non-zero value.
func (p *printer) format(graph uint64, depth int, encoded wire.Value) (string, bool) {
	x := encoded
	switch x.Kind() {
	case wire.KindNil:
		return "nil", false
	case wire.KindNoObjects:
		return "{}", false
	case wire.KindString:
		return fmt.Sprintf("%q", x.Text()), x.Text() != ""
	case wire.KindComplex64, wire.KindComplex128:
		value := x.Complex()
		return fmt.Sprintf("%f+%fi", real(value), imag(value)), value != 0
	case wire.KindRef:
		ref := x.ReferenceView()
		return p.formatRef(ref, graph), ref.Root() != 0
	case wire.KindType:
		x := x.Type()
		tabs := "\n" + strings.Repeat("\t", depth)
		items := make([]string, 0, len(x.Fields)+2)
		items = append(items, fmt.Sprintf("type %s {", x.Name))
		for i := 0; i < len(x.Fields); i++ {
			items = append(items, fmt.Sprintf("\t%d: %s,", i, x.Fields[i]))
		}
		items = append(items, "}")
		return strings.Join(items, tabs), true // No zero value.
	case wire.KindSlice:
		slice := x.SliceView()
		return fmt.Sprintf("%s{len:%d,cap:%d}", p.formatRef(slice.Ref, graph), slice.Length, slice.Capacity), slice.Capacity != 0
	case wire.KindArray, wire.KindMultipleObjects:
		if x.Len() == 0 {
			return "[]", false
		}
		items := make([]string, 0, x.Len()+2)
		zeros := make([]string, 0) // used to eliminate zero entries.
		items = append(items, "[")
		tabs := "\n" + strings.Repeat("\t", depth)
		for i := 0; i < x.Len(); i++ {
			item, ok := p.format(graph, depth+1, x.Element(i))
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
		return strings.Join(items, tabs), len(zeros) < x.Len()
	case wire.KindStruct:
		tag := fmt.Sprintf("g%dt%d", graph, x.StructType())
		spec := p.typeSpecs[tag]
		typ, _ := p.formatTypeID(x.StructType(), graph)
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
			element, ok := p.format(graph, depth+1, x.Field(i))
			allZero = allZero && !ok
			items = append(items, fmt.Sprintf("\t%s: %s,", name, element))
		}
		items = append(items, "}")
		return strings.Join(items, tabs), !allZero
	case wire.KindMap:
		if x.Len() == 0 {
			return "map{}", false
		}
		items := make([]string, 0, x.Len()+2)
		items = append(items, "map{")
		tabs := "\n" + strings.Repeat("\t", depth)
		for i := 0; i < x.Len(); i++ {
			keyValue, entryValue := x.Entry(i)
			key, _ := p.format(graph, depth+1, keyValue)
			value, _ := p.format(graph, depth+1, entryValue)
			items = append(items, fmt.Sprintf("\t%s: %s,", key, value))
		}
		items = append(items, "}")
		return strings.Join(items, tabs), true
	case wire.KindInterface:
		typeView, value := x.InterfaceView()
		typ, typOk := p.formatType(typeView, graph)
		element, elementOk := p.format(graph, depth+1, value)
		return fmt.Sprintf("interface[%s]{%s}", typ, element), typOk || elementOk
	case wire.KindBool:
		return fmt.Sprintf("%v", x.Bool()), true
	case wire.KindInt:
		return fmt.Sprintf("%v", x.Int()), true
	case wire.KindUint:
		return fmt.Sprintf("%v", x.Uint()), true
	case wire.KindFloat32:
		return fmt.Sprintf("%v", float32(x.Float())), true
	case wire.KindFloat64:
		return fmt.Sprintf("%v", x.Float()), true
	default:
		panic(fmt.Sprintf("unreachable: unknown value kind %v", x.Kind()))
	}
}

// printStream is the basic print implementation.
func (p *printer) printStream(w io.Writer, r io.Reader) (err error) {
	wr := wire.Reader{Reader: r}
	var values wire.ValueBuffer
	defer values.Release()

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
			obj wire.Value
		}
		var (
			tid     uint64 = 1
			objects []objectAndID
		)
		for i := uint64(0); i < length; {
			// Unmarshal either a type object or object ID.
			encoded := values.Load(&wr)
			switch encoded.Kind() {
			case wire.KindType:
				we := encoded.Type()
				str, _ := p.format(graph, 0, encoded)
				tag := fmt.Sprintf("g%dt%d", graph, tid)
				p.typeSpecs[tag] = we
				if p.html {
					// See below.
					tag = fmt.Sprintf("<a name=\"%s\">%s</a><a href=\"#%s\">&#9875;</a>", tag, tag, tag)
				}
				if _, err := fmt.Fprintf(w, "%s = %s\n", tag, str); err != nil {
					return err
				}
				tid++
			case wire.KindUint:
				// Unmarshal the actual object.
				id := encoded.Uint()
				objects = append(objects, objectAndID{
					id:  id,
					obj: values.Load(&wr).Own(),
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
