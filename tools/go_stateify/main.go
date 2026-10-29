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
	"go/format"
	"go/parser"
	"go/token"
	"log"
	"maps"
	"os"
	"path"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"

	"gvisor.dev/gvisor/tools/constraintutil"
)

var (
	fullPkg  = flag.String("fullpkg", "", "fully qualified output package")
	imports  = flag.String("imports", "", "extra imports for the output file")
	output   = flag.String("output", "", "output file")
	statePkg = flag.String("statepkg", "", "state import package; defaults to empty")
)

// fileImports resolves the package names a generated type expression can use.
// A source import takes precedence over a package supplied for struct tags.
func fileImports(f *ast.File, extra map[string]string) map[string]string {
	names := maps.Clone(extra)
	for _, spec := range f.Imports {
		p, err := strconv.Unquote(spec.Path.Value)
		if err != nil {
			log.Fatal(err)
		}
		name := path.Base(p)
		if spec.Name != nil {
			name = spec.Name.Name
		}
		names[name] = p
	}
	return names
}

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

// scanFunctions is a set of functions passed to scanFields.
type scanFunctions struct {
	zerovalue func(name string)
	normal    func(name string)
	wait      func(name string)
	value     func(name, typName string)
}

// scanFields scans the fields of a struct.
//
// Each provided function will be applied to appropriately tagged fields, or
// skipped if nil.
//
// Fields tagged nosave are skipped.
func scanFields(ss *ast.StructType, fn scanFunctions) {
	if ss.Fields.List == nil {
		// No fields.
		return
	}

	// Scan all fields.
	for _, field := range ss.Fields.List {
		if field.Names == nil {
			// Anonymous types can't be embedded, so we don't need
			// to worry about providing a useful name here.
			name, _ := resolveTypeName(field.Type)
			scanField(name, field, fn)
			continue
		}

		// Iterate over potentially multiple fields defined on the same line.
		for _, nameI := range field.Names {
			name := nameI.Name
			// Skip _ fields.
			if name == "_" {
				continue
			}
			scanField(name, field, fn)
		}
	}
}

// scanField scans a single struct field with a resolved name.
func scanField(name string, field *ast.Field, fn scanFunctions) {
	// Is this a anonymous struct? If yes, then continue the
	// recursion with the given prefix. We don't pay attention to
	// any tags on the top-level struct field.
	tag := extractStateTag(field.Tag)
	if anon, ok := field.Type.(*ast.StructType); ok && tag == "" {
		scanFields(anon, fn)
		return
	}

	switch tag {
	case "zerovalue":
		if fn.zerovalue != nil {
			fn.zerovalue(name)
		}

	case "":
		if fn.normal != nil {
			fn.normal(name)
		}

	case "wait":
		if fn.wait != nil {
			fn.wait(name)
		}

	case "manual", "nosave", "ignore":
		// Do nothing.

	default:
		if strings.HasPrefix(tag, ".(") && strings.HasSuffix(tag, ")") {
			if fn.value != nil {
				fn.value(name, tag[2:len(tag)-1])
			}
		}
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

	log.SetFlags(0)
	var header bytes.Buffer
	outputFile := new(bytes.Buffer)
	used := make(map[string]string)
	use := func(name, p string) {
		if old, ok := used[name]; ok && old != p {
			log.Fatalf("generated package name %q refers to both %q and %q", name, old, p)
		}
		used[name] = p
	}
	extra := make(map[string]string)
	if *imports != "" {
		for p := range strings.SplitSeq(*imports, ",") {
			extra[path.Base(p)] = p
		}
	}

	// Set the statePrefix for below, depending on the import.
	statePrefix := ""
	if *statePkg != "" {
		statePrefix = path.Base(*statePkg) + "."
	}

	// initCalls is dumped at the end.
	var initCalls []string

	// Common closures.
	emitRegister := func(name string) {
		if *statePkg != "" {
			use(path.Base(*statePkg), *statePkg)
		}
		initCalls = append(initCalls, fmt.Sprintf("%sRegister((*%s)(nil))", statePrefix, name))
	}

	// Automated warning.
	fmt.Fprint(&header, "// automatically generated by stateify.\n\n")

	// Emit build constraints.
	bcexpr, err := constraintutil.CombineFromFiles(flag.Args())
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to infer build constraints: %v", err)
		os.Exit(1)
	}
	header.WriteString(constraintutil.Lines(bcexpr))

	// Emit the package name.
	_, pkg := filepath.Split(*fullPkg)
	fmt.Fprintf(&header, "package %s\n\n", pkg)

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

	for _, f := range files {
		names := fileImports(f, extra)
		useType := func(typ ast.Expr) {
			for node := range ast.Preorder(typ) {
				sel, ok := node.(*ast.SelectorExpr)
				if !ok {
					continue
				}
				id, ok := sel.X.(*ast.Ident)
				if !ok {
					continue
				}
				p, ok := names[id.Name]
				if !ok {
					log.Fatalf("no import for %q in generated type; declare its path with -imports", id.Name)
				}
				use(id.Name, p)
			}
		}
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
					if isIdentType {
						fmt.Fprintf(os.Stderr, "Cannot use `+stateify identtype` on a struct type (%v); must be a type definition of an identical type.", ts.Name.Name)
						os.Exit(1)
					}

					// Record the slot for each field.
					fieldCount := 0
					fields := make(map[string]int)
					emitField := func(name string) {
						fmt.Fprintf(outputFile, "		\"%s\",\n", name)
						fields[name] = fieldCount
						fieldCount++
					}
					emitFieldValue := func(name string, _ string) {
						emitField(name)
					}
					emitLoadValue := func(name, typName string) {
						typ, err := parser.ParseExpr(typName)
						if err != nil {
							log.Fatalf("invalid state type for %s.%s: %v", ts.Name.Name, name, err)
						}
						useType(typ)
						fmt.Fprintf(outputFile, "	stateSourceObject.LoadValue(%d, new(%s), func(y any) { %s.load%s(ctx, y.(%s)) })\n", fields[name], typName, recv, camelCased(name), typName)
					}
					emitLoad := func(name string) {
						fmt.Fprintf(outputFile, "	stateSourceObject.Load(%d, &%s.%s)\n", fields[name], recv, name)
					}
					emitLoadWait := func(name string) {
						fmt.Fprintf(outputFile, "	stateSourceObject.LoadWait(%d, &%s.%s)\n", fields[name], recv, name)
					}
					emitSaveValue := func(name, typName string) {
						// Keep an explicit typName ascription as a compile-time
						// check against code generation bugs while avoiding S1021.
						fmt.Fprintf(outputFile, "	%sValue := %s.save%s()\n", name, recv, camelCased(name))
						fmt.Fprintf(outputFile, "	_ = (%s)(%sValue)\n", typName, name)
						fmt.Fprintf(outputFile, "	stateSinkObject.SaveValue(%d, %sValue)\n", fields[name], name)
					}
					emitSave := func(name string) {
						fmt.Fprintf(outputFile, "	stateSinkObject.Save(%d, &%s.%s)\n", fields[name], recv, name)
					}
					emitZeroCheck := func(name string) {
						fmt.Fprintf(outputFile, "	if !%sIsZeroValue(&%s.%s) { %sFailf(\"%s is %%#v, expected zero\", &%s.%s) }\n", statePrefix, recv, name, statePrefix, name, recv, name)
					}

					// Generate the type name method.
					fmt.Fprintf(outputFile, "func (%s *%s) StateTypeName() string {\n", recv, ts.Name.Name)
					fmt.Fprintf(outputFile, "	return \"%s.%s\"\n", *fullPkg, ts.Name.Name)
					fmt.Fprintf(outputFile, "}\n\n")

					// Generate the fields method.
					fmt.Fprintf(outputFile, "func (%s *%s) StateFields() []string {\n", recv, ts.Name.Name)
					fmt.Fprintf(outputFile, "	return []string{\n")
					scanFields(x, scanFunctions{
						normal: emitField,
						wait:   emitField,
						value:  emitFieldValue,
					})
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
					if generateSaverLoader {
						fmt.Fprintf(outputFile, "// +checklocksignore\n")
						fmt.Fprintf(outputFile, "func (%s *%s) StateSave(stateSinkObject %sSink) {\n", recv, ts.Name.Name, statePrefix)
						fmt.Fprintf(outputFile, "	%s.beforeSave()\n", recv)
						scanFields(x, scanFunctions{zerovalue: emitZeroCheck})
						scanFields(x, scanFunctions{value: emitSaveValue})
						scanFields(x, scanFunctions{normal: emitSave, wait: emitSave})
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
						use("context", "context")
						fmt.Fprintf(outputFile, "func (%s *%s) StateLoad(ctx context.Context, stateSourceObject %sSource) {\n", recv, ts.Name.Name, statePrefix)
						scanFields(x, scanFunctions{normal: emitLoad, wait: emitLoadWait})
						scanFields(x, scanFunctions{value: emitLoadValue})
						if hasAfterLoad {
							// The call to afterLoad is made conditionally, because when
							// AfterLoad is called, the object encodes a dependency on
							// referred objects (i.e. fields). This means that afterLoad
							// will not be called until the other afterLoads are called.
							fmt.Fprintf(outputFile, "	stateSourceObject.AfterLoad(func () { %s.afterLoad(ctx) })\n", recv)
						}
						fmt.Fprintf(outputFile, "}\n\n")
					}

					// Add to our registration.
					emitRegister(ts.Name.Name)

				case *ast.Ident, *ast.SelectorExpr, *ast.ArrayType:

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
							useType(y)
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
							use("context", "context")
							fmt.Fprintf(outputFile, "func (%s *%s) StateLoad(ctx context.Context, stateSourceObject %sSource) {\n", recv, ts.Name.Name, statePrefix)
							fmt.Fprintf(outputFile, "	(*%s)(%s).StateLoad(ctx, stateSourceObject)\n", typeName, recv)
							fmt.Fprintf(outputFile, "}\n\n")
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

	if len(used) != 0 {
		header.WriteString("import (\n")
		for _, standard := range []bool{true, false} {
			for name, p := range used {
				first, _, _ := strings.Cut(p, "/")
				isStandard := !strings.Contains(first, ".")
				if isStandard != standard {
					continue
				}
				if name != path.Base(p) {
					fmt.Fprintf(&header, "%s ", name)
				}
				fmt.Fprintf(&header, "%q\n", p)
			}
			header.WriteByte('\n')
		}
		header.WriteString(")\n")
	}
	header.Write(outputFile.Bytes())
	src, err := format.Source(header.Bytes())
	if err != nil {
		log.Fatalf("format generated Go: %v", err)
	}
	if *output == "" || *output == "-" {
		_, err = os.Stdout.Write(src)
	} else {
		err = os.WriteFile(*output, src, 0644)
	}
	if err != nil {
		log.Fatal(err)
	}
}
