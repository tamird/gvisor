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

// Stateify provides a simple way to generate Load/Save methods based on
// existing types and struct tags.
package main

import (
	"bytes"
	"flag"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"

	"gvisor.dev/gvisor/tools/constraintutil"
)

var (
	fullPkg  = flag.String("fullpkg", "", "fully qualified output package")
	imports  = flag.String("imports", "", "extra imports for the output file")
	output   = flag.String("output", "", "output file")
	statePkg = flag.String("statepkg", "", "state import package; defaults to empty")
)

// resolveTypeName returns a qualified type name.
func resolveTypeName(typ ast.Expr) (field string, qualified string) {
	for done := false; !done; {
		// Resolve star expressions.
		switch rs := typ.(type) {
		case *ast.StarExpr:
			qualified += "*"
			typ = rs.X
		case *ast.ArrayType:
			if rs.Len == nil {
				// Slice type declaration.
				qualified += "[]"
			} else {
				// Array type declaration.
				qualified += "[" + rs.Len.(*ast.BasicLit).Value + "]"
			}
			typ = rs.Elt
		default:
			// No more descent.
			done = true
		}
	}

	// Resolve a package selector.
	sel, ok := typ.(*ast.SelectorExpr)
	if ok {
		qualified = qualified + sel.X.(*ast.Ident).Name + "."
		typ = sel.Sel
	}

	// Figure out actual type name.
	field = typ.(*ast.Ident).Name
	qualified = qualified + field
	return
}

// extractStateTag pulls the relevant state tag.
func extractStateTag(tag *ast.BasicLit) string {
	if tag == nil {
		return ""
	}
	if len(tag.Value) < 2 {
		return ""
	}
	return reflect.StructTag(tag.Value[1 : len(tag.Value)-1]).Get("state")
}

// fieldKind is the kind of a saved field.
type fieldKind int

const (
	fieldNormal fieldKind = iota
	fieldWait
	fieldValue
)

// fieldInfo describes a saved field, in slot order.
type fieldInfo struct {
	name string
	typ  ast.Expr
	kind fieldKind
	// valueType is the type of the value for fieldValue.
	valueType string
}

// structFields separates serialized slots from assertions that consume no slot.
type structFields struct {
	saved []fieldInfo
	zero  []string
}

func collectFields(ss *ast.StructType) structFields {
	var fields structFields
	fields.collect(ss)
	return fields
}

func (fields *structFields) collect(ss *ast.StructType) {
	for _, field := range ss.Fields.List {
		for _, ident := range field.Names {
			if ident.Name != "_" {
				fields.add(ident.Name, field)
			}
		}
		if field.Names == nil {
			name, _ := resolveTypeName(field.Type)
			fields.add(name, field)
		}
	}
}

func (fields *structFields) add(name string, field *ast.Field) {
	tag := extractStateTag(field.Tag)
	f := fieldInfo{name: name, typ: field.Type}
	switch {
	case tag == "":
		if anon, ok := field.Type.(*ast.StructType); ok {
			fields.collect(anon)
			return
		}
	case tag == "wait":
		f.kind = fieldWait
	case tag == "zerovalue":
		fields.zero = append(fields.zero, name)
		return
	case strings.HasPrefix(tag, ".(") && strings.HasSuffix(tag, ")"):
		f.kind, f.valueType = fieldValue, tag[2:len(tag)-1]
	default:
		return
	}
	fields.saved = append(fields.saved, f)
}

func (fields *structFields) emitZeroChecks(w io.Writer, sp, recv string) {
	for _, name := range fields.zero {
		fmt.Fprintf(w, "\tif !%sIsZeroValue(&%s.%s) { %sFailf(\"%s is %%#v, expected zero\", &%s.%s) }\n", sp, recv, name, sp, name, recv, name)
	}
}

func camelCased(name string) string {
	return strings.ToUpper(name[:1]) + name[1:]
}

func main() {
	// Parse flags.
	flag.Usage = func() {
		fmt.Fprintf(os.Stderr, "Usage: %s [options]\n", os.Args[0])
		flag.PrintDefaults()
	}
	flag.Parse()
	if len(flag.Args()) == 0 {
		flag.Usage()
		os.Exit(1)
	}
	if *fullPkg == "" {
		fmt.Fprintf(os.Stderr, "Error: package required.")
		os.Exit(1)
	}

	// Open the output file.
	var (
		outputFile *os.File
		err        error
	)
	if *output == "" || *output == "-" {
		outputFile = os.Stdout
	} else {
		outputFile, err = os.OpenFile(*output, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0644)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error opening output %q: %v", *output, err)
		}
		defer outputFile.Close()
	}

	// Set the statePrefix for below, depending on the import.
	statePrefix := ""
	if *statePkg != "" {
		parts := strings.Split(*statePkg, "/")
		statePrefix = parts[len(parts)-1] + "."
	}

	// initCalls is dumped at the end.
	var initCalls []string

	// Common closures.
	emitRegister := func(name string) {
		initCalls = append(initCalls, fmt.Sprintf("%sRegister((*%s)(nil))", statePrefix, name))
	}

	// Automated warning.
	fmt.Fprint(outputFile, "// automatically generated by stateify.\n\n")

	// Emit build constraints.
	bcexpr, err := constraintutil.CombineFromFiles(flag.Args())
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to infer build constraints: %v", err)
		os.Exit(1)
	}
	outputFile.WriteString(constraintutil.Lines(bcexpr))

	// Emit the package name.
	_, pkg := filepath.Split(*fullPkg)
	fmt.Fprintf(outputFile, "package %s\n\n", pkg)

	// Emit the imports lazily.
	maybeEmitImports := sync.OnceFunc(func() {
		// Emit the imports.
		fmt.Fprint(outputFile, "import (\n")
		fmt.Fprint(outputFile, " \"context\"\n")
		if *statePkg != "" {
			fmt.Fprintf(outputFile, "	\"%s\"\n", *statePkg)
		}
		if *imports != "" {
			for _, i := range strings.Split(*imports, ",") {
				fmt.Fprintf(outputFile, "	\"%s\"\n", i)
			}
		}
		fmt.Fprint(outputFile, ")\n\n")
	})

	files := make([]*ast.File, 0, len(flag.Args()))

	// Parse the input files.
	for _, filename := range flag.Args() {
		// Parse the file.
		fset := token.NewFileSet()
		f, err := parser.ParseFile(fset, filename, nil, parser.ParseComments)
		if err != nil {
			// Not a valid input file?
			fmt.Fprintf(os.Stderr, "Input %q can't be parsed: %v\n", filename, err)
			os.Exit(1)
		}

		files = append(files, f)
	}

	type method struct {
		typeName   string
		methodName string
	}

	// Search for and add all method to a set. We auto-detecting several
	// different methods (and insert them if we don't find them, in order
	// to ensure that expectations match reality).
	//
	// While we do this, figure out the right receiver name. If there are
	// multiple distinct receivers, then we will just pick the last one.
	simpleMethods := make(map[method]struct{})
	receiverNames := make(map[string]string)
	for _, f := range files {
		// Go over all functions.
		for _, decl := range f.Decls {
			d, ok := decl.(*ast.FuncDecl)
			if !ok {
				continue
			}
			if d.Recv == nil || len(d.Recv.List) != 1 {
				// Not a named method.
				continue
			}

			// Save the method and the receiver.
			name, _ := resolveTypeName(d.Recv.List[0].Type)
			simpleMethods[method{
				typeName:   name,
				methodName: d.Name.Name,
			}] = struct{}{}
			if len(d.Recv.List[0].Names) > 0 {
				receiverNames[name] = d.Recv.List[0].Names[0].Name
			}
		}
	}

	// Codec methods are emitted at the end. See below.
	var codecOut bytes.Buffer

	// Record all type declarations, for codec generation.
	cg := &codecGen{local: make(localTypes), statePrefix: statePrefix}
	for _, f := range files {
		for _, decl := range f.Decls {
			if d, ok := decl.(*ast.GenDecl); ok && d.Tok == token.TYPE {
				for _, gs := range d.Specs {
					cg.add(gs.(*ast.TypeSpec))
				}
			}
		}
	}

	for _, f := range files {
		// Go over all named types.
		for _, decl := range f.Decls {
			d, ok := decl.(*ast.GenDecl)
			if !ok || d.Tok != token.TYPE {
				continue
			}

			// Only generate code for types marked "// +stateify
			// savable" in one of the proceeding comment lines. If
			// the line is marked "// +stateify type" then only
			// generate type information and register the type.
			// If the type also has a "// +stateify identtype"
			// comment, the functions are instead generated to refer to
			// the type that this newly-defined type is identical to, rather
			// than about the newly-defined type itself.
			if d.Doc == nil {
				continue
			}
			var (
				generateTypeInfo    = false
				generateSaverLoader = false
				isIdentType         = false
			)
			for _, l := range d.Doc.List {
				if l.Text == "// +stateify savable" {
					generateTypeInfo = true
					generateSaverLoader = true
				}
				if l.Text == "// +stateify type" {
					generateTypeInfo = true
				}
				if l.Text == "// +stateify identtype" {
					isIdentType = true
				}
			}
			if !generateTypeInfo && !generateSaverLoader {
				continue
			}

			for _, gs := range d.Specs {
				ts := gs.(*ast.TypeSpec)
				recv, ok := receiverNames[ts.Name.Name]
				if !ok {
					// Maybe no methods were defined?
					recv = strings.ToLower(ts.Name.Name[:1])
				}
				switch x := ts.Type.(type) {
				case *ast.StructType:
					maybeEmitImports()
					if isIdentType {
						fmt.Fprintf(os.Stderr, "Cannot use `+stateify identtype` on a struct type (%v); must be a type definition of an identical type.", ts.Name.Name)
						os.Exit(1)
					}

					fields := collectFields(x)

					// Generate the type name method.
					fmt.Fprintf(outputFile, "func (%s *%s) StateTypeName() string {\n", recv, ts.Name.Name)
					fmt.Fprintf(outputFile, "	return \"%s.%s\"\n", *fullPkg, ts.Name.Name)
					fmt.Fprintf(outputFile, "}\n\n")

					// Generate the fields method.
					fmt.Fprintf(outputFile, "func (%s *%s) StateFields() []string {\n", recv, ts.Name.Name)
					fmt.Fprintf(outputFile, "	return []string{\n")
					for _, f := range fields.saved {
						fmt.Fprintf(outputFile, "\t\t%q,\n", f.name)
					}
					fmt.Fprintf(outputFile, "	}\n")
					fmt.Fprintf(outputFile, "}\n\n")

					// Define beforeSave if a definition was not found. This prevents
					// the code from compiling if a custom beforeSave was defined in a
					// file not provided to this binary and prevents inherited methods
					// from being called multiple times by overriding them.
					if _, ok := simpleMethods[method{
						typeName:   ts.Name.Name,
						methodName: "beforeSave",
					}]; !ok && generateSaverLoader {
						fmt.Fprintf(outputFile, "func (%s *%s) beforeSave() {}\n\n", recv, ts.Name.Name)
					}

					// Generate the save method.
					//
					// N.B. For historical reasons, we perform the value saves first,
					// and perform the value loads last. There should be no dependency
					// on this specific behavior, but the ability to specify slots
					// allows a manual implementation to be order-dependent.
					shared := cg.sharedLeaf(fields)
					inlineFields := fields.saved
					if shared {
						inlineFields = nil
					}
					if generateSaverLoader {
						fmt.Fprintf(outputFile, "// +checklocksignore\n")
						fmt.Fprintf(outputFile, "func (%s *%s) StateSave(stateSinkObject %sSink) {\n", recv, ts.Name.Name, statePrefix)
						fmt.Fprintf(outputFile, "	%s.beforeSave()\n", recv)
						fields.emitZeroChecks(outputFile, statePrefix, recv)
						if shared {
							fmt.Fprintf(outputFile, "\t%s.stateVisitFields(&stateSinkObject)\n", recv)
						}
						for slot, f := range inlineFields {
							if f.kind == fieldValue {
								fmt.Fprintf(outputFile, "\t%sValue := %s.save%s()\n", f.name, recv, camelCased(f.name))
								fmt.Fprintf(outputFile, "\t_ = (%s)(%sValue)\n", f.valueType, f.name)
								fmt.Fprintf(outputFile, "\tstateSinkObject.SaveValue(%d, %sValue)\n", slot, f.name)
							}
						}
						for slot, f := range inlineFields {
							if f.kind != fieldValue {
								fmt.Fprintf(outputFile, "\tstateSinkObject.Save(%d, &%s.%s)\n", slot, recv, f.name)
							}
						}
						fmt.Fprintf(outputFile, "}\n\n")
					}

					// Define afterLoad if a definition was not found. We do this for
					// the same reason that we do it for beforeSave.
					_, hasAfterLoad := simpleMethods[method{
						typeName:   ts.Name.Name,
						methodName: "afterLoad",
					}]
					if !hasAfterLoad && generateSaverLoader {
						fmt.Fprintf(outputFile, "func (%s *%s) afterLoad(context.Context) {}\n\n", recv, ts.Name.Name)
					}

					// Generate the load method.
					//
					// N.B. See the comment above for the save method.
					if generateSaverLoader {
						fmt.Fprintf(outputFile, "// +checklocksignore\n")
						fmt.Fprintf(outputFile, "func (%s *%s) StateLoad(ctx context.Context, stateSourceObject %sSource) {\n", recv, ts.Name.Name, statePrefix)
						if shared {
							fmt.Fprintf(outputFile, "\t%s.stateVisitFields(&stateSourceObject)\n", recv)
						}
						for slot, f := range inlineFields {
							switch f.kind {
							case fieldNormal:
								fmt.Fprintf(outputFile, "\tstateSourceObject.Load(%d, &%s.%s)\n", slot, recv, f.name)
							case fieldWait:
								fmt.Fprintf(outputFile, "\tstateSourceObject.LoadWait(%d, &%s.%s)\n", slot, recv, f.name)
							}
						}
						for slot, f := range inlineFields {
							if f.kind == fieldValue {
								fmt.Fprintf(outputFile, "\tstateSourceObject.LoadValue(%d, new(%s), func(y any) { %s.load%s(ctx, y.(%s)) })\n", slot, f.valueType, recv, camelCased(f.name), f.valueType)
							}
						}
						if hasAfterLoad {
							// The call to afterLoad is made conditionally, because when
							// AfterLoad is called, the object encodes a dependency on
							// referred objects (i.e. fields). This means that afterLoad
							// will not be called until the other afterLoads are called.
							fmt.Fprintf(outputFile, "	stateSourceObject.AfterLoad(func () { %s.afterLoad(ctx) })\n", recv)
						}
						fmt.Fprintf(outputFile, "}\n\n")

						// Generate the typed codec methods.
						cg.emitCodec(&codecOut, recv, ts.Name.Name, fields, hasAfterLoad)
					}

					// Add to our registration.
					emitRegister(ts.Name.Name)

				case *ast.Ident, *ast.SelectorExpr, *ast.ArrayType:
					maybeEmitImports()

					// Generate the info methods.
					fmt.Fprintf(outputFile, "func (%s *%s) StateTypeName() string {\n", recv, ts.Name.Name)
					fmt.Fprintf(outputFile, "	return \"%s.%s\"\n", *fullPkg, ts.Name.Name)
					fmt.Fprintf(outputFile, "}\n\n")

					if !isIdentType {
						fmt.Fprintf(outputFile, "func (%s *%s) StateFields() []string {\n", recv, ts.Name.Name)
						fmt.Fprintf(outputFile, "	return nil\n")
						fmt.Fprintf(outputFile, "}\n\n")
					} else {
						var typeName string
						switch y := x.(type) {
						case *ast.Ident:
							typeName = y.Name
						case *ast.SelectorExpr:
							expIdent, ok := y.X.(*ast.Ident)
							if !ok {
								fmt.Fprintf(os.Stderr, "Cannot use non-ident %v (type %T) in type selector expression %v", y.X, y.X, y)
								os.Exit(1)
							}
							typeName = fmt.Sprintf("%s.%s", expIdent.Name, y.Sel.Name)
						default:
							fmt.Fprintf(os.Stderr, "Cannot use `+stateify identtype` on a non-identifier/non-selector type definition (%v => %v of type %T); must be a type definition of an identical type.", ts.Name.Name, x, x)
							os.Exit(1)
						}
						fmt.Fprintf(outputFile, "func (%s *%s) StateFields() []string {\n", recv, ts.Name.Name)
						fmt.Fprintf(outputFile, "	return (*%s)(%s).StateFields()\n", typeName, recv)
						fmt.Fprintf(outputFile, "}\n\n")
						if generateSaverLoader {
							fmt.Fprintf(outputFile, "// +checklocksignore\n")
							fmt.Fprintf(outputFile, "func (%s *%s) StateSave(stateSinkObject %sSink) {\n", recv, ts.Name.Name, statePrefix)
							fmt.Fprintf(outputFile, "	(*%s)(%s).StateSave(stateSinkObject)\n", typeName, recv)
							fmt.Fprintf(outputFile, "}\n\n")
							fmt.Fprintf(outputFile, "// +checklocksignore\n")
							fmt.Fprintf(outputFile, "func (%s *%s) StateLoad(ctx context.Context, stateSourceObject %sSource) {\n", recv, ts.Name.Name, statePrefix)
							fmt.Fprintf(outputFile, "	(*%s)(%s).StateLoad(ctx, stateSourceObject)\n", typeName, recv)
							fmt.Fprintf(outputFile, "}\n\n")
							cg.emitIdentCodec(&codecOut, recv, ts.Name.Name, typeName)
						}
					}

					// See above.
					emitRegister(ts.Name.Name)
				}
			}
		}
	}

	if len(initCalls) > 0 {
		// Emit the init() function.
		fmt.Fprintf(outputFile, "func init() {\n")
		for _, ic := range initCalls {
			fmt.Fprintf(outputFile, "	%s\n", ic)
		}
		fmt.Fprintf(outputFile, "}\n")
	}

	// Emit the codec methods last, leaving the code above unchanged.
	if codecOut.Len() > 0 {
		fmt.Fprintf(outputFile, "\n")
		outputFile.Write(codecOut.Bytes())
	}
}
