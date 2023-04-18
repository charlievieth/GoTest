package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"go/ast"
	"go/build"
	"go/parser"
	"go/token"
	"io"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/charlievieth/buildutil"
	"github.com/charlievieth/buildutil/contextutil"
	"github.com/spf13/cobra"
	util "golang.org/x/tools/go/buildutil"
)

var version = "development"

type TestVisitor struct {
	mu         sync.Mutex
	Tests      []*ast.FuncDecl
	Benchmarks []*ast.FuncDecl
	Examples   []*ast.FuncDecl
	Fuzz       []*ast.FuncDecl
}

func (v *TestVisitor) AddTest(d *ast.FuncDecl) {
	v.mu.Lock()
	v.Tests = append(v.Tests, d)
	v.mu.Unlock()
}

func (v *TestVisitor) AddBenchmark(d *ast.FuncDecl) {
	v.mu.Lock()
	v.Benchmarks = append(v.Benchmarks, d)
	v.mu.Unlock()
}

func (v *TestVisitor) AddExample(d *ast.FuncDecl) {
	v.mu.Lock()
	v.Examples = append(v.Examples, d)
	v.mu.Unlock()
}

func (v *TestVisitor) AddFuzz(d *ast.FuncDecl) {
	v.mu.Lock()
	v.Fuzz = append(v.Fuzz, d)
	v.mu.Unlock()
}

func (v *TestVisitor) Visit(node ast.Node) (w ast.Visitor) {
	if d, ok := node.(*ast.FuncDecl); ok && d != nil && d.Name != nil {
		switch name := d.Name.Name; {
		case strings.HasPrefix(name, "Test"):
			v.AddTest(d)
		case strings.HasPrefix(name, "Benchmark"):
			v.AddBenchmark(d)
		case strings.HasPrefix(name, "Example"):
			v.AddExample(d)
		case strings.HasPrefix(name, "Fuzz"):
			v.AddFuzz(d)
		}
	}
	return v
}

type FuncDefinition struct {
	Name     string `json:"name"`
	Filename string `json:"filename"`
	Line     int    `json:"line"`
	Doc      string `json:"comment,omitempty"`
}

func declsToDefinitions(fset *token.FileSet, decls []*ast.FuncDecl) []*FuncDefinition {
	if len(decls) == 0 {
		return nil
	}
	defs := make([]*FuncDefinition, len(decls))
	for i, d := range decls {
		pos := fset.Position(d.Pos())
		defs[i] = &FuncDefinition{
			Name:     d.Name.Name,
			Filename: pos.Filename,
			Line:     pos.Line,
			Doc:      d.Doc.Text(),
		}
	}
	sort.Slice(defs, func(i, j int) bool {
		return defs[i].Name < defs[j].Name
	})
	return defs
}

type ListTestsResponse struct {
	PkgName    string            `json:"pkg_name"`
	PkgRoot    string            `json:"pkg_root"`
	GoEnv      *GoEnv            `json:"go_env,omitempty"`
	Tests      []*FuncDefinition `json:"tests,omitempty"`
	Benchmarks []*FuncDefinition `json:"benchmarks,omitempty"`
	Examples   []*FuncDefinition `json:"examples,omitempty"`
	Fuzz       []*FuncDefinition `json:"fuzz,omitempty"`
}

// TODO: list funcs and methods as well
func ListTests(ctxt *build.Context, dir string) (*ListTestsResponse, error) {
	pkg, err := ctxt.ImportDir(dir, 0)
	if err != nil {
		return nil, err
	}

	// TODO: log the error?
	pkgRoot, _ := contextutil.FindProjectRoot(ctxt, dir)
	if pkgRoot == "" {
		pkgRoot = filepath.Clean(dir)
	}

	names := append(pkg.TestGoFiles, pkg.XTestGoFiles...)
	if len(names) == 0 {
		return &ListTestsResponse{PkgName: pkg.Name, PkgRoot: pkgRoot}, nil
	}

	errs := make([]error, len(names))
	fset := token.NewFileSet()
	v := new(TestVisitor)
	wg := new(sync.WaitGroup)

	for i, name := range names {
		wg.Add(1)
		go func(i int, name string) {
			defer wg.Done()
			af, err := util.ParseFile(fset, ctxt, nil, dir, name,
				parser.ParseComments)
			if err != nil {
				errs[i] = err
			} else {
				ast.Walk(v, af)
			}
		}(i, name)
	}
	wg.Wait()

	for _, err := range errs {
		if err != nil {
			return nil, err
		}
	}

	res := &ListTestsResponse{
		PkgName:    pkg.Name,
		PkgRoot:    pkgRoot,
		GoEnv:      DiffGoEnv(&build.Default, ctxt),
		Tests:      declsToDefinitions(fset, v.Tests),
		Benchmarks: declsToDefinitions(fset, v.Benchmarks),
		Examples:   declsToDefinitions(fset, v.Examples),
		Fuzz:       declsToDefinitions(fset, v.Fuzz),
	}
	return res, nil
}

type NoContainingFunctionError struct {
	Filename string `json:"filename"`
	Line     int    `json:"line"`
	Column   int    `json:"column"`
}

func (e *NoContainingFunctionError) Error() string {
	return fmt.Sprintf("no containing function at: %s:%d:%d",
		e.Filename, e.Line, e.Column)
}

type FuncVisitor struct {
	Pos token.Pos
	Fn  *ast.FuncDecl
}

func (v *FuncVisitor) Visit(node ast.Node) (w ast.Visitor) {
	if v.Fn != nil {
		return nil
	}
	if d, ok := node.(*ast.FuncDecl); ok && d != nil {
		if d.Pos() <= v.Pos && v.Pos <= d.End() {
			v.Fn = d
			return nil
		}
	}
	return v
}

// TODO: use `findcall -name NAME *.go` to find references
// where findcall is "golang.org/x/tools/go/analysis/passes/findcall/cmd/findcall"
func ContainingFunction(filename string, src interface{}, line, column int) (string, error) {
	fset := token.NewFileSet()
	af, err := parser.ParseFile(fset, filename, src, parser.SkipObjectResolution)
	if err != nil && af == nil {
		return "", err
	}

	file := fset.File(af.Pos())
	if file == nil {
		return "", errors.New("ast: no pos for file")
	}
	if n := file.LineCount(); line < 1 || line > n {
		return "", fmt.Errorf("ast: invalid line number %d (should be between 1 and %d)", line, n)
	}
	pos := file.LineStart(line)
	if !pos.IsValid() {
		return "", fmt.Errorf("ast: invalid pos for line: %d", line)
	}

	// Fast check
	for _, node := range af.Decls {
		if d, ok := node.(*ast.FuncDecl); ok && d != nil {
			if d.Pos() <= pos && pos <= d.End() {
				if d.Name != nil {
					return d.Name.Name, nil
				}
			}
		}
	}

	v := FuncVisitor{Pos: pos}
	ast.Walk(&v, af)

	if v.Fn != nil && v.Fn.Name != nil {
		return v.Fn.Name.Name, nil
	}
	return "", &NoContainingFunctionError{filename, line, column}
}

type TestConfig struct {
	Verbose bool
	Short   bool
	Race    bool
}

type Event struct {
	Time    *time.Time `json:",omitempty"`
	Action  string
	Package string   `json:",omitempty"`
	Test    string   `json:",omitempty"`
	Elapsed *float64 `json:",omitempty"`
	Output  *string  `json:",omitempty"`
}

// func Test2JsonExe(ctxt *build.Context) (string, error) {
// 	goroot := runtime.GOROOT()
// 	if !sameFile(ctxt.GOROOT, goroot) {
// 		exe, err := exec.LookPath(filepath.Join(
// 			ctxt.GOROOT, "pkg", "tool", runtime.GOOS+"_"+runtime.GOARCH, "test2json",
// 		))
// 		if err == nil {
// 			return exe, nil
// 		}
// 	}
// 	return exec.LookPath(filepath.Join(
// 		goroot, "pkg", "tool", runtime.GOOS+"_"+runtime.GOARCH, "test2json",
// 	))
// }

func RunTests(ctxt *build.Context, dirname string, args ...string) ([]Event, error) {
	// test2json := filepath.Join(runtime.GOROOT(), "pkg", "tool", runtime.GOOS+"_"+runtime.GOARCH, "test2json")
	// tmpdir, err := os.MkdirTemp("", "gotest-util-*")
	// if err != nil {
	// 	return nil, err
	// }
	// defer os.RemoveAll(tmpdir)
	//
	// stdout, err := os.Create(tmpdir + "/stdout.out")
	// if err != nil {
	// 	return nil, err
	// }
	// defer stdout.Close()
	//
	// stderr, err := os.Create(tmpdir + "/stderr.out")
	// if err != nil {
	// 	return nil, err
	// }
	// defer stderr.Close()

	var stdout bytes.Buffer
	cmd := buildutil.GoCommand(ctxt, "go", append([]string{"test"}, args...)...)
	cmd.Dir = dirname
	cmd.Stdout = &stdout
	cmd.Stderr = os.Stderr

	if err := cmd.Run(); err != nil {
		return nil, err // WARN: include STDERR
	}

	return nil, nil
}

func MatchContext(orig *build.Context, filename string) (*build.Context, error) {
	ctxt, err := buildutil.MatchContext(orig, filename, nil)
	if ctxt != nil {
		ctxt.Dir = filepath.Dir(filename)
	}
	return ctxt, err
}

func stringsEqual(a1, a2 []string) bool {
	if len(a1) != len(a2) {
		return false
	}
	for i := range a1 {
		if a1[i] != a2[i] {
			goto tryUnordered
		}
	}
	return true

tryUnordered:
	// Ignore order
	m := make(map[string]bool, len(a1))
	for _, s := range a1 {
		m[s] = true
	}
	for _, s := range a2 {
		if !m[s] {
			return false
		}
	}
	return true
}

type GoEnv struct {
	GoArch       *string `json:"GOARCH,omitempty"`
	GoHostArch   *string `json:"GOHOSTARCH,omitempty"`
	GoOS         *string `json:"GOOS,omitempty"`
	GoHostOS     *string `json:"GOHOSTOS,omitempty"`
	GoRoot       *string `json:"GOROOT,omitempty"`
	GoPath       *string `json:"GOPATH,omitempty"`
	CgoEnabled   *string `json:"CGO_ENABLED,omitempty"`
	GoFlags      *string `json:"GOFLAGS,omitempty"`
	GoExperiment *string `json:"GOEXPERIMENT,omitempty"`
	// WARN: new
	// GoTags       []string `json:"GOTAGS,omitempty"`
}

func DiffGoEnv(orig, ctxt *build.Context) *GoEnv {
	p := func(s string) *string {
		return &s
	}
	e := new(GoEnv)
	if ctxt.GOARCH != orig.GOARCH || ctxt.GOARCH != runtime.GOARCH {
		e.GoArch = p(ctxt.GOARCH)
		e.GoHostArch = p(runtime.GOARCH)
	}
	if ctxt.GOOS != orig.GOOS || ctxt.GOOS != runtime.GOOS {
		e.GoOS = p(ctxt.GOOS)
		e.GoHostOS = p(runtime.GOOS)
	}
	if ctxt.GOROOT != orig.GOROOT {
		e.GoRoot = p(ctxt.GOROOT)
	}
	if ctxt.GOPATH != orig.GOPATH {
		e.GoPath = p(ctxt.GOPATH)
	}
	if ctxt.CgoEnabled != orig.CgoEnabled {
		e.CgoEnabled = p(strconv.FormatBool(ctxt.CgoEnabled))
	}
	if !stringsEqual(ctxt.BuildTags, orig.BuildTags) {
		// TODO: this is actually "build tags"
		e.GoFlags = p(strings.Join(ctxt.BuildTags, ","))
	}
	if !stringsEqual(ctxt.ToolTags, orig.ToolTags) {
		e.GoExperiment = p(strings.Join(ctxt.ToolTags, ","))
	}
	return e
}

// func DiffContexts(orig, ctxt *build.Context) map[string]string {
// 	m := make(map[string]string)
// 	if ctxt.GOARCH != orig.GOARCH {
// 		m["GOARCH"] = ctxt.GOARCH
// 		m["GOHOSTARCH"] = runtime.GOARCH
// 	}
// 	if ctxt.GOOS != orig.GOOS {
// 		m["GOOS"] = ctxt.GOOS
// 		m["GOHOSTOS"] = runtime.GOOS
// 	}
// 	if ctxt.GOROOT != orig.GOROOT {
// 		m["GOROOT"] = ctxt.GOROOT
// 	}
// 	if ctxt.GOPATH != orig.GOPATH {
// 		m["GOPATH"] = ctxt.GOPATH
// 	}
// 	if ctxt.CgoEnabled != orig.CgoEnabled {
// 		m["CGO_ENABLED"] = strconv.FormatBool(ctxt.CgoEnabled)
// 	}
// 	if !stringsEqual(ctxt.BuildTags, orig.BuildTags) {
// 		// WARN: this is actually "build tags"
// 		m["GOFLAGS"] = strings.Join(ctxt.BuildTags, ",")
// 	}
// 	if !stringsEqual(ctxt.ToolTags, orig.ToolTags) {
// 		m["GOEXPERIMENT"] = strings.Join(ctxt.ToolTags, ",")
// 	}
// 	return m
// }

func CopyContext(orig *build.Context) *build.Context {
	if orig == nil {
		orig = &build.Default
	}
	dupe := *orig
	dupe.BuildTags = append([]string(nil), orig.BuildTags...)
	dupe.ToolTags = append([]string(nil), orig.ToolTags...)
	dupe.ReleaseTags = append([]string(nil), orig.ReleaseTags...)
	return &dupe
}

// OverlayContext overlays a build.Context with additional files from
// a map. Files in the map take precedence over other files.
//
// In addition to plain string comparison, two file names are
// considered equal if their base names match and their directory
// components point at the same directory on the file system. That is,
// symbolic links are followed for directories, but not files.
//
// A common use case for OverlayContext is to allow editors to pass in
// a set of unsaved, modified files.
//
// Currently, only the Context.OpenFile function will respect the
// overlay. This may change in the future.
func OverlayContext(orig *build.Context, overlay map[string]string) *build.Context {
	// TODO(dominikh): Implement IsDir, HasSubdir and ReadDir

	copy := *orig // make a copy
	ctxt := &copy
	ctxt.OpenFile = func(path string) (io.ReadCloser, error) {
		// Fast path: names match exactly.
		if content, ok := overlay[path]; ok {
			return io.NopCloser(strings.NewReader(content)), nil
		}

		// Slow path: check for same file under a different
		// alias, perhaps due to a symbolic link.
		for filename, content := range overlay {
			if sameFile(path, filename) {
				return io.NopCloser(strings.NewReader(content)), nil
			}
		}

		return util.OpenFile(orig, path)
	}
	return ctxt
}

// sameFile returns true if x and y have the same basename and denote
// the same file.
func sameFile(x, y string) bool {
	if x == y {
		return true
	}
	if filepath.Base(x) == filepath.Base(y) {
		if path.Clean(x) == path.Clean(y) {
			return true
		}
		if xi, err := os.Stat(x); err == nil {
			if yi, err := os.Stat(y); err == nil {
				return os.SameFile(xi, yi)
			}
		}
	}
	return false
}

func ParseFileQuery(query string) (*token.Position, error) {
	s := query

	i := strings.LastIndexByte(s, ':')
	if i == -1 {
		return nil, errors.New("invalid file query: missing column")
	}
	col, err := strconv.Atoi(s[i+1:])
	if err != nil {
		return nil, fmt.Errorf("invalid file query: parsing column: %w", err)
	}
	s = s[:i]

	i = strings.LastIndexByte(s, ':')
	if i == -1 {
		return nil, errors.New("invalid file query: missing line")
	}
	line, err := strconv.Atoi(s[i+1:])
	if err != nil {
		return nil, fmt.Errorf("invalid file query: parsing line: %w", err)
	}

	name := s[:i]
	return &token.Position{Filename: name, Line: line, Column: col}, nil
}

// // WARN: use or remove
// type ErrorWriter struct {
// 	w io.Writer
// }
//
// // WARN: use or remove
// func (e *ErrorWriter) Write(p []byte) (int, error) {
// 	json.NewEncoder(e.w).Encode(struct {
// 		Error string `json:"error"`
// 	}{string(p)})
// 	return len(p), nil
// }

type OverlayJSON struct {
	Replace map[string]string `json:"replace"`
}

// WARN: remove if not used
func isFile(ctxt *build.Context, name string) bool {
	if ctxt != nil && ctxt.OpenFile != nil {
		f, err := ctxt.OpenFile(name)
		if err != nil {
			return false
		}
		f.Close()
		return true
	}
	fi, err := os.Stat(name)
	return err == nil && fi.Mode().IsRegular()
}

func main() {
	ctxt := CopyContext(&build.Default)
	ctxt.HasSubdir = contextutil.HasSubdirFunc(ctxt)

	root := cobra.Command{
		Use: "gotest-util",
		PersistentPreRunE: func(cmd *cobra.Command, _ []string) error {
			overlay, err := cmd.Flags().GetString("overlay")
			if err != nil {
				return err // should never happen
			}
			if strings.TrimSpace(overlay) == "" {
				return nil
			}
			var o OverlayJSON
			dec := json.NewDecoder(strings.NewReader(overlay))
			dec.DisallowUnknownFields()
			if err := dec.Decode(&o); err != nil {
				return err
			}
			if len(o.Replace) > 0 {
				ctxt = OverlayContext(ctxt, o.Replace)
			}
			return nil
		},
	}
	root.SilenceUsage = true

	// TODO: create Context from flags
	flags := root.PersistentFlags()
	flags.String("tags", "", "build tags")
	flags.String("overlay", "",
		"read a JSON config file that provides an overlay for build operations")
	flags.Bool("race", false, "enable race detection")

	listCmd := cobra.Command{
		Use:   "list [FILE]",
		Short: "List runnable Go tests",
		Args:  cobra.MaximumNArgs(1),
		RunE: func(_ *cobra.Command, args []string) (err error) {
			dirname := "."
			// If a file is provided match the context to it.
			if len(args) == 1 {
				dirname = filepath.Dir(args[0])
				ctxt, err = MatchContext(ctxt, args[0])
				if err != nil {
					return err
				}
			}
			dirname, err = filepath.Abs(dirname)
			if err != nil {
				return err
			}

			defs, err := ListTests(ctxt, dirname)
			if err != nil {
				return err
			}

			// WARN WARN WARN
			// enc := json.NewEncoder(os.Stdout)
			// enc.SetIndent("", "    ")
			// return enc.Encode(defs)
			// WARN WARN WARN

			return json.NewEncoder(os.Stdout).Encode(defs)
		},
	}

	envCmd := cobra.Command{
		Use:     "env FILE",
		Aliases: []string{"environment"},
		Short:   "Print the Go environment matching FILE",
		Args:    cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			ctxt, err := MatchContext(ctxt, args[0])
			if err != nil {
				return err
			}
			env := DiffGoEnv(&build.Default, ctxt)
			return json.NewEncoder(os.Stdout).Encode(env)
		},
	}

	funcCmd := cobra.Command{
		Use:     "function FILE_QUERY",
		Short:   "Print the function containing the cursor",
		Example: fmt.Sprintf("%s function ./main.go:12:8", filepath.Base(os.Args[0])),
		Args:    cobra.ExactArgs(1),
		RunE: func(_ *cobra.Command, args []string) error {
			pos, err := ParseFileQuery(args[0])
			if err != nil {
				return err
			}

			// Handle file overlays
			var src []byte
			f, err := util.OpenFile(ctxt, pos.Filename)
			if err != nil {
				return err
			}
			src, err = io.ReadAll(f)
			f.Close()
			if err != nil {
				return err
			}

			// Return any error here as part of the JSON response.
			funcName, err := ContainingFunction(pos.Filename, src, pos.Line, pos.Column)
			var errMsg string
			if err != nil {
				errMsg = err.Error()
			}
			return json.NewEncoder(os.Stdout).Encode(struct {
				Name  string `json:"name"`
				Error string `json:"error,omitempty"`
			}{funcName, errMsg})
		},
	}

	versionCmd := cobra.Command{
		Use:   "version",
		Short: "Print the tool version and exit",
		Args:  cobra.NoArgs,
		RunE: func(_ *cobra.Command, _ []string) error {
			_, err := fmt.Println(version)
			return err
		},
	}

	root.AddCommand(&listCmd, &envCmd, &funcCmd, &versionCmd)

	if err := root.Execute(); err != nil {
		os.Exit(1)
	}
}
