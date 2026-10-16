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
	fullPkg     string
	imports     string
	statePkg    string
	typedConfig = flag.String("config", "", "declared package inputs")
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
	if *typedConfig == "" {
		flag.Usage()
		os.Exit(1)
	}
	if err := generateRecords(*typedConfig); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

// parseFiles uses one file set for the package and its generated methods.
func parseFiles(fset *token.FileSet, names []string) ([]*ast.File, error) {
	files := make([]*ast.File, 0, len(names))
	for _, name := range names {
		f, err := parser.ParseFile(fset, name, nil, parser.ParseComments)
		if err != nil {
			return nil, fmt.Errorf("parse %s: %w", name, err)
		}
		files = append(files, f)
	}
	return files, nil
}

// render emits the production saver from the ordered field model. The internal
// type-checking scaffold contains declarations, not an alternative saver.
// Loading, hook order and field numbering share this model.
func render(outputFile io.Writer, names []string, group *groupModel, records *recordSet, scaffold bool) {
	// Set the statePrefix for below, depending on the import.
	statePrefix := ""
	if statePkg != "" {
		parts := strings.Split(statePkg, "/")
		statePrefix = parts[len(parts)-1] + "."
	}

	// initCalls is dumped at the end.
	var initCalls []string

	// Common closures.
	emitRegister := func(name string, generated bool) {
		register := "Register"
		if generated {
			register = "RegisterGenerated"
		}
		initCalls = append(initCalls, fmt.Sprintf("%s%s((*%s)(nil))", statePrefix, register, name))
	}

	emitMetadata := func(name, recv string) bool {
		if records == nil {
			return false
		}
		record := records.types[name]
		forward := records.forward[name]
		if record == nil && forward == "" {
			return false
		}
		capture := "nil"
		owner := fmt.Sprintf("(*%s)(nil)", name)
		metadataRecv := ""
		if record != nil {
			capture = record.factory
		} else if forward != "" {
			metadataRecv = recv
			if statePrefix == recv+"." || typeUsesName(forward, recv) || recv == "true" || recv == "false" {
				metadataRecv = records.saveReceiver
			}
			capture = fmt.Sprintf("%sForwardRecord((*%s)(%s))", statePrefix, forward, metadataRecv)
			// The receiver may shadow its own type name. Its actual pointer
			// supplies the same owner type without another type expression.
			owner = metadataRecv
		}
		// Ordinary metadata does not use the receiver. Forwarding retains the
		// owning type's receiver name, subject to the same scope constraints.
		fmt.Fprintf(outputFile, "func (%s *%s) StateMetadata() %sTypeMetadata {\n", metadataRecv, name, statePrefix)
		fmt.Fprintf(outputFile, " return %sTypeMetadata{Owner: %s, NewRecord: %s}\n}\n\n", statePrefix, owner, capture)
		return true
	}

	// Automated warning.
	fmt.Fprint(outputFile, "// automatically generated by stateify.\n\n")

	// Emit build constraints.
	bcexpr, err := constraintutil.CombineFromFiles(names)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to infer build constraints: %v", err)
		os.Exit(1)
	}
	fmt.Fprint(outputFile, constraintutil.Lines(bcexpr))

	// Emit the package name.
	_, pkg := filepath.Split(fullPkg)
	fmt.Fprintf(outputFile, "package %s\n\n", pkg)

	// Emit the imports lazily.
	maybeEmitImports := sync.OnceFunc(func() {
		// Emit the imports.
		fmt.Fprint(outputFile, "import (\n")
		fmt.Fprint(outputFile, " \"context\"\n")
		if statePkg != "" {
			fmt.Fprintf(outputFile, "	\"%s\"\n", statePkg)
		}
		if records != nil && len(records.types) != 0 {
			fmt.Fprintf(outputFile, " %s \"gvisor.dev/gvisor/pkg/state/wire\"\n", records.wireAlias)
		}
		if imports != "" {
			for _, i := range strings.Split(imports, ",") {
				fmt.Fprintf(outputFile, "	\"%s\"\n", i)
			}
		}
		fmt.Fprint(outputFile, ")\n\n")
	})

	simpleMethods := group.methods
	for _, model := range group.types {
		ts := model.spec
		recv := model.receiver
		generateSaverLoader := model.save
		isIdentType := model.identical
		switch x := ts.Type.(type) {
		case *ast.StructType:
			maybeEmitImports()
			if isIdentType {
				fmt.Fprintf(os.Stderr, "Cannot use `+stateify identtype` on a struct type (%v); must be a type definition of an identical type.", ts.Name.Name)
				os.Exit(1)
			}

			// Record the slot for each field.
			fields := make(map[string]int)
			for _, field := range model.fields {
				if field.slot >= 0 {
					fields[field.name] = field.slot
				}
			}
			emitField := func(name string) {
				fmt.Fprintf(outputFile, "		\"%s\",\n", name)
			}
			emitFieldValue := func(name string, _ string) {
				emitField(name)
			}
			loadRecv := recv
			if records != nil {
				// Typed field calls and their wait arguments must not be
				// shadowed by the receiver.
				shadowed := statePrefix == recv+"." || recv == "true" || recv == "false"
				if statePrefix == "" {
					for _, helper := range records.restore.fields[ts.Name.Name] {
						shadowed = shadowed || recv == helper
					}
				}
				if shadowed {
					loadRecv = records.saveReceiver
				}
				// Custom value type spellings also occur in this method.
				for _, field := range model.fields {
					if field.kind == fieldValue && typeUsesName(field.valueType, recv) {
						loadRecv = records.saveReceiver
						break
					}
				}
			}
			emitLoadValue := func(name, typName string) {
				fmt.Fprintf(outputFile, " stateSourceObject.LoadValue(%d, new(%s), func(y any) { %s.load%s(ctx, y.(%s)) })\n", fields[name], typName, loadRecv, camelCased(name), typName)
			}
			// Checked capture can introduce package-qualified calls inside
			// StateSave. Keep a handwritten receiver from shadowing that
			// import, without renaming the hook or other generated methods.
			saveRecv := recv
			if records != nil && statePrefix == recv+"." {
				saveRecv = records.saveReceiver
			}
			recordLocal := "stateSnapshotObject"
			if recv == recordLocal {
				recordLocal += "1"
			}
			var record *recordType
			if records != nil {
				record = records.types[ts.Name.Name]
			}
			emitSaveValue := func(name, typName string) {
				// Keep an explicit typName ascription as a compile-time
				// check against code generation bugs while avoiding S1021.
				fmt.Fprintf(outputFile, "	%sValue := %s.save%s()\n", name, saveRecv, camelCased(name))
				fmt.Fprintf(outputFile, "	_ = (%s)(%sValue)\n", typName, name)
				record.capture(outputFile, records.wireAlias, statePrefix, recordLocal, fields[name], name+"Value", true)
			}
			emitSave := func(name string) {
				record.capture(outputFile, records.wireAlias, statePrefix, recordLocal, fields[name], saveRecv+"."+name, false)
			}
			emitZeroCheck := func(name string) {
				fmt.Fprintf(outputFile, "	if !%sIsZeroValue(&%s.%s) { %sFailf(\"%s is %%#v, expected zero\", &%s.%s) }\n", statePrefix, saveRecv, name, statePrefix, name, saveRecv, name)
			}

			// Generate the type name method.
			fmt.Fprintf(outputFile, "func (%s *%s) StateTypeName() string {\n", recv, ts.Name.Name)
			fmt.Fprintf(outputFile, "	return \"%s.%s\"\n", fullPkg, ts.Name.Name)
			fmt.Fprintf(outputFile, "}\n\n")

			// Generate the fields method.
			fmt.Fprintf(outputFile, "func (%s *%s) StateFields() []string {\n", recv, ts.Name.Name)
			fmt.Fprintf(outputFile, "	return []string{\n")
			model.scan(scanFunctions{
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
			_, hasStateSave := simpleMethods[method{
				typeName:   ts.Name.Name,
				methodName: "StateSave",
			}]
			if generateSaverLoader && hasStateSave {
				// A direct declaration owns capture and hooks. Let the
				// compiler check its complete method signature.
				fmt.Fprintf(outputFile, "var _ %sSaverLoader = (*%s)(nil)\n\n", statePrefix, ts.Name.Name)
			}
			if generateSaverLoader && !hasStateSave {
				if record != nil {
					record.declare(outputFile, records.wireAlias)
				}
				fmt.Fprintf(outputFile, "// +checklocksignore\n")
				fmt.Fprintf(outputFile, "func (%s *%s) StateSave(stateSinkObject %sSink) {\n", saveRecv, ts.Name.Name, statePrefix)
				if !scaffold {
					if record == nil {
						panic("generated saver has no checked record")
					}
					fmt.Fprintf(outputFile, "	%s.beforeSave()\n", saveRecv)
					model.scan(scanFunctions{zerovalue: emitZeroCheck})
					if len(record.fields) != 0 {
						fmt.Fprintf(outputFile, " %s := %sRecord(stateSinkObject, %d, %s).(*%s)\n", recordLocal, statePrefix, len(record.fields), record.factory, record.name)
					}
					model.scan(scanFunctions{value: emitSaveValue})
					model.scan(scanFunctions{normal: emitSave, wait: emitSave})
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
				fmt.Fprintf(outputFile, "func (%s *%s) StateLoad(ctx context.Context, stateSourceObject %sSource) {\n", loadRecv, ts.Name.Name, statePrefix)
				if !scaffold {
					for _, field := range model.fields {
						if field.kind != fieldNormal && field.kind != fieldWait {
							continue
						}
						wait := records.restore.noWaitValue
						if field.kind == fieldWait {
							wait = records.restore.waitValue
						}
						if helper := records.restore.fields[ts.Name.Name][field.name]; helper != "" {
							fmt.Fprintf(outputFile, " %s%s(stateSourceObject, %d, &%s.%s, %s)\n", statePrefix, helper, field.slot, loadRecv, field.name, wait)
						} else {
							method := "Load"
							if field.kind == fieldWait {
								method = "LoadWait"
							}
							fmt.Fprintf(outputFile, " stateSourceObject.%s(%d, &%s.%s)\n", method, field.slot, loadRecv, field.name)
						}
					}
					model.scan(scanFunctions{value: emitLoadValue})
					if hasAfterLoad {
						// The call to afterLoad is made conditionally, because when
						// AfterLoad is called, the object encodes a dependency on
						// referred objects (i.e. fields). This means that afterLoad
						// will not be called until the other afterLoads are called.
						fmt.Fprintf(outputFile, "	stateSourceObject.AfterLoad(func () { %s.afterLoad(ctx) })\n", loadRecv)
					}
				}
				fmt.Fprintf(outputFile, "}\n\n")
			}

			// Add to our registration.
			emitRegister(ts.Name.Name, emitMetadata(ts.Name.Name, recv))

		case *ast.Ident, *ast.SelectorExpr, *ast.ArrayType:
			maybeEmitImports()

			// Generate the info methods.
			fmt.Fprintf(outputFile, "func (%s *%s) StateTypeName() string {\n", recv, ts.Name.Name)
			fmt.Fprintf(outputFile, "	return \"%s.%s\"\n", fullPkg, ts.Name.Name)
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
				}
			}

			// See above.
			emitRegister(ts.Name.Name, emitMetadata(ts.Name.Name, recv))
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
}

// typeUsesName checks lexical type-expression names, not selector members.
// Retained custom LoadValue types are emitted inside the receiver's scope.
func typeUsesName(expression, name string) bool {
	expr, err := parser.ParseExpr(expression)
	if err != nil {
		panic(fmt.Sprintf("invalid checked custom type %q: %v", expression, err))
	}
	found := false
	var visit func(ast.Node) bool
	visit = func(node ast.Node) bool {
		switch node := node.(type) {
		case *ast.Ident:
			found = found || node.Name == name
		case *ast.Field:
			ast.Inspect(node.Type, visit)
			return false
		case *ast.SelectorExpr:
			ast.Inspect(node.X, visit)
			return false
		}
		return true
	}
	ast.Inspect(expr, visit)
	return found
}
