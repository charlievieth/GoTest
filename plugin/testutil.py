from pprint import pprint

import hashlib
import os
from re import sub
import re
import subprocess
import json

from functools import lru_cache
from functools import wraps
from tempfile import NamedTemporaryFile
from threading import RLock
from typing import Any
from typing import Callable
from typing import cast
from typing import Dict
from typing import List
from typing import Optional
from typing import TypedDict
from typing import TypeVar

# WARN WARN WARN WARN
# WARN DEV ONLY
# WARN WARN WARN WARN
try:
    from .logger import get_logger
except ImportError:
    from logger import get_logger

logger = get_logger("testutil")

# WARN WARN WARN WARN
# WARN DEV ONLY
# WARN WARN WARN WARN
try:
    # import sublime
    from sublime import Region
    from sublime import View
    from sublime import packages_path
except ModuleNotFoundError:

    def packages_path() -> str:
        return os.path.abspath(os.path.join(__file__, "../tmp"))

    class View:
        pass

_mswindows = os.name == "nt"

_gotest_check_lock = RLock()
_gotest_util_checked = False  # checked if binary is up to date
_gotest_util_installed = False  # installed binary
_gotest_expected_version: Optional[str] = None

_DEBUG_CMD = False

_PROJECT_ROOT = os.path.abspath(os.path.join(__file__, "../../"))
_GOTEST_CMD_DIR = os.path.join(_PROJECT_ROOT, "cmd", "gotest-util")
_GOTEST_UTIL_EXE = os.path.join(
    packages_path(),
    "User",
    "GoTest",
    "bin",
    "gotest-util" if not _mswindows else "gotest-util.exe",
)


def _check_output(
    args: List[str],
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
    rstrip: bool = True,
) -> str:

    if os.name == _mswindows:
        # Hide the console window on Windows
        startupinfo = subprocess.STARTUPINFO()  # type: ignore
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore
        preexec_fn = None
    else:
        startupinfo = None
        preexec_fn = os.setsid

    output = subprocess.check_output(
        args,
        cwd=cwd,
        timeout=timeout,
        stderr=subprocess.STDOUT,
        startupinfo=startupinfo,
        preexec_fn=preexec_fn,
        encoding="utf-8",
    )
    return output.rstrip() if output and rstrip else output


@lru_cache(maxsize=8)
def _go_version(goexe: str = "go") -> str:
    version = _check_output([goexe, "version"])
    for prefix in ["go version devel ", "go version "]:
        if version.startswith(prefix):
            version = version[len(prefix):]
    return version.split(" ")[0]


def _hash_go_files() -> str:
    go_files = []
    for root, dirs, files in os.walk(_PROJECT_ROOT):
        # prune uninteresting directories
        for x in [".git", ".mypy_cache", "vendor", "plugin"]:
            if x in dirs:
                del dirs[dirs.index(x)]
        for file in files:
            if file.endswith(".go") or file == "go.mod" or file == "go.sum":
                go_files.append(os.path.join(root, file))

    m = hashlib.sha256()
    for file in sorted(go_files):
        try:
            with open(file) as f:
                m.update(file.encode())
                m.update(f.read().encode())
        except FileNotFoundError:
            pass

    return m.hexdigest()[:8]


def _expected_version(goexe: str = "go") -> str:
    global _gotest_expected_version
    if _gotest_expected_version is None or _DEBUG_CMD:
        _gotest_expected_version = _go_version(goexe) + "-" + _hash_go_files()
    return _gotest_expected_version


# TODO: run in another thread
def _install_gotest_util(goexe: str = "go") -> None:
    expected_version = _expected_version()
    testexe = _GOTEST_UTIL_EXE

    # Make sure the bin dir exists
    os.makedirs(os.path.dirname(testexe), exist_ok=True)

    temp = NamedTemporaryFile(suffix=".exe", prefix=testexe + "-").name
    try:
        ldflags = f"-ldflags=-X main.version={expected_version}"
        _check_output(
            [goexe, "build", ldflags, "-o", temp],
            cwd=_GOTEST_CMD_DIR,
        )
        current_version = _check_output([temp, "version"])
        if current_version != expected_version:
            raise RuntimeError(
                (
                    f"gotest-util: expected version: {expected_version} "
                    + f"got version: {current_version}"
                ),
            )

        # Overwrite the old exe
        os.rename(temp, testexe)

    except subprocess.CalledProcessError as e:
        # pretty print build failure
        out = [s for s in e.stdout.split("\n") if s and not s.startswith("#")]
        logger.exception("building gotest-util stderr: %s", "\n".join(out))

    finally:
        if os.path.exists(temp):
            os.remove(temp)


# TODO: use or remove
def _should_build_gotest_util(goexe: str = "go") -> bool:
    if not os.path.exists(_GOTEST_UTIL_EXE):
        logger.info("need rebuild: gotest-util: not installed")
        return True

    expected_version = _expected_version(goexe)
    current_version = _check_output([_GOTEST_UTIL_EXE, "version"])
    if expected_version != current_version:
        logger.info(
            "need rebuild: gotest-util: outdated (%s -> %s)",
            current_version, expected_version,
        )
        return True

    return False


def check_gotest_util(goexe: str = "go") -> None:
    global _gotest_util_checked
    global _gotest_util_installed

    with _gotest_check_lock:
        if not _gotest_util_checked or _DEBUG_CMD:
            _gotest_util_checked = True
            logger.info("checking gotest-util")
            if _should_build_gotest_util(goexe):
                logger.info("rebuilding gotest-util")
                _install_gotest_util()
                _gotest_util_installed = True

            # if (
            #     not os.path.exists(_GOTEST_UTIL_EXE) or
            #     _check_output([_GOTEST_UTIL_EXE, "version"]) != _expected_version(goexe)
            # ):
            #     logger.info("rebuilding gotest-util")
            #     _install_gotest_util()
            #     _gotest_util_installed = True


class RawFuncDefinition(TypedDict):
    name: str
    filename: str
    line: int
    doc: Optional[str]


class RawListResponse(TypedDict):
    tests: Optional[List[RawFuncDefinition]]
    benchmarks: Optional[List[RawFuncDefinition]]
    examples: Optional[List[RawFuncDefinition]]
    fuzz: Optional[List[RawFuncDefinition]]


class FuncDefinition:
    __slots__ = "name", "filename", "line", "doc"

    def __init__(
        self,
        name: str,
        filename: str,
        line: int,
        doc: Optional[str] = None,
    ) -> None:
        self.name = name
        self.filename = filename
        self.line = line
        self.doc = doc

    def __repr__(self) -> str:
        args = []
        for attr in self.__slots__:
            v = getattr(self, attr)
            if v is not None:
                args.append(f"{attr}={v!r}")
        return f"{self.__class__.__name__}({', '.join(args)})"

    @classmethod
    def from_raw(cls, raw: RawFuncDefinition) -> "FuncDefinition":
        return FuncDefinition(
            name=raw["name"],
            filename=raw["filename"],
            line=raw["line"],
            doc=raw.get("doc"),
        )

    def to_raw(self) -> RawFuncDefinition:
        return {
            "name": self.name,
            "filename": self.filename,
            "line": self.line,
            "doc": self.doc,
        }


class ListResponse:
    __slots__ = "tests", "benchmarks", "examples", "fuzz"

    def __init__(
        self,
        tests: Optional[List[FuncDefinition]] = None,
        benchmarks: Optional[List[FuncDefinition]] = None,
        examples: Optional[List[FuncDefinition]] = None,
        fuzz: Optional[List[FuncDefinition]] = None,
    ) -> None:
        self.tests = tests
        self.benchmarks = benchmarks
        self.examples = examples
        self.fuzz = fuzz

    def __repr__(self) -> str:
        args = []
        for attr in self.__slots__:
            v = getattr(self, attr)
            if v is not None:
                args.append(f"{attr}={v!r}")
        return f"{self.__class__.__name__}({', '.join(args)})"

    @classmethod
    def from_raw(cls, raw: Optional[RawListResponse]) -> "ListResponse":
        if raw is None:
            return ListResponse()

        def convert(raw: RawListResponse, key: str) -> Optional[List[FuncDefinition]]:
            v = cast(Optional[List[RawFuncDefinition]], raw.get(key, None))
            return [FuncDefinition.from_raw(d) for d in v] if v else None

        return ListResponse(
            tests=convert(raw, "tests"),
            benchmarks=convert(raw, "benchmarks"),
            examples=convert(raw, "examples"),
            fuzz=convert(raw, "fuzz"),
        )

    def to_raw(self) -> RawListResponse:
        return {
            "tests": [d.to_raw() for d in self.tests or []],
            "benchmarks": [d.to_raw() for d in self.benchmarks or []],
            "examples": [d.to_raw() for d in self.examples or []],
            "fuzz": [d.to_raw() for d in self.fuzz or []],
        }


FuncT = TypeVar('FuncT', bound=Callable[..., Any])


def requires_gotest_exe(fn: FuncT) -> FuncT:
    # https://mypy.readthedocs.io/en/stable/generics.html#declaring-decorators
    @wraps(fn)
    def wrapper(*args, **kwds):
        check_gotest_util()
        return fn(*args, **kwds)
    return cast(FuncT, wrapper)


# class Overlay:
#     __slots__ = "replace"
#
#     def __init__(self, replace: Optional[Dict[str, str]] = None) -> None:
#         self.replace = replace
#
#     def add(self, filename: str, source: str) -> None:
#         if self.replace is not None:
#             self.replace[filename] = source
#         else:
#             self.replace = {filename: source}
#
#     def add_view(self, view: sublime.View) -> None:
#         name = view.file_name()
#         source = view_src(view)
#         if name and source:
#             self.add(name, source)
#
#     @classmethod
#     def from_views(cls, views: List[sublime.view]) -> "Optional[Overlay]":
#         replace = {}
#         for view in views:
#             if view is None or view.is_scratch() or not view.is_dirty():
#                 continue
#             name = view.file_name()
#             if name:
#                 replace[name] = view_src(view)
#         if replace:
#             return Overlay(replace)
#         return None


def view_src(view: View) -> str:
    """Returns the string source of the Sublime view.
    """
    return view.substr(Region(0, view.size())) if view else ""


def overlay(views: List[View]) -> Optional[Dict[str, str]]:
    replace: Optional[Dict[str, str]] = None
    for view in views:
        if view is not None and view.is_dirty() and not view.is_scratch():
            name = view.file_name()
            if name:
                if replace:
                    replace[name] = view_src(view)
                else:
                    replace = {name: view_src(view)}
    return replace


# WARN: need to rebuild on change
#
# TODO: include func and method names as well so that we can better
# match tests
@requires_gotest_exe
def list_tests(
    filename: str,
    overlay: Optional[Dict[str, str]] = None,
) -> ListResponse:
    if overlay:
        extra = ["--overlay", json.dumps({"replace": overlay})]
    else:
        extra = []
    data = _check_output(
        [_GOTEST_UTIL_EXE] + extra + ["list", filename],
        cwd=os.path.dirname(filename),
    )
    return ListResponse.from_raw(json.loads(data))

    # print("#########")
    # print(data)
    # print("#########")
    # dec = json.JSONDecoder()
    # tests: List[RawFuncDefinition] = []
    # while data:
    #     t, n = dec.raw_decode(data)
    #     data = data[n:]
    #     tests.append(t)
    # return [FuncDefinition.from_raw(test) for test in tests]
    # # except subprocess.CalledProcessError as e:


XXX_TEST = """
package strings

import "testing"

func TestXXX(t *testing.T) {
    t.Fatal("WAT")
}
"""

BAD_TEST = """
asdads
"""

if __name__ == "__main__":
    import sys
    check_gotest_util()
    # check_gotest_util()

    name = "/Users/cvieth/Projects/go-dev/src/strings/clone.go"
    # overlay = {
    #     os.path.join(os.path.dirname(name), "clone_test.go"): XXX_TEST,
    # }
    tests = list_tests(name)
    json.dump(tests.to_raw(), sys.stdout, indent=4)
    # pprint(tests)

    # print(len(tests.tests))
    # pprint(tests)

    # tests = list_tests(name, overlay=overlay)
    # json.dump(tests, sys.stdout, indent=4)
