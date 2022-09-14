import json
import os
import re
import subprocess

from enum import Enum
from typing import Any
from typing import AnyStr
from typing import Dict
from typing import KeysView
from typing import List
from typing import NamedTuple
from typing import Optional
from typing import TypedDict
from typing import cast
from subprocess import CompletedProcess

# from typing import NamedTuple
# from typing import TypedDict

from .plugin.logger import get_logger
from .plugin.exec import AsyncProcess

import sublime
import sublime_plugin


logger = get_logger()

REPLACE_FILENAME_LINE_RE = re.compile(r"(^\s*\w+\.go:\d+: )")
FILENAME_LINE_FAILURE_RE = re.compile(r"^[    ]{1,}(\w+\.go):(\d+)\:\s+(.*)")
TEST_REPORT_RE = re.compile(r"^[    ]{0,}--- (PASS|FAIL|SKIP|BENCH): \w+")
TEST_UPDATE_RE = re.compile(r"^=== (RUN|PAUSE|CONT)\s+")


class LineError(NamedTuple):
    filename: str
    line: int
    message: str


def parse_line_error(line: str) -> Optional[LineError]:
    m = FILENAME_LINE_FAILURE_RE.match(line)
    if m is not None:
        return LineError(m[1], int(m[2]), m[3])
    return None


class TestAction(Enum):
    RUN = "run"
    PAUSE = "pause"
    CONT = "cont"
    PASS = "pass"
    BENCH = "bench"
    FAIL = "fail"
    OUTPUT = "output"
    SKIP = "skip"

    @classmethod
    def from_string(cls, s: str) -> "TestAction":
        for e in cls:
            if e.value == s:
                return e
        raise ValueError(f"invalid TestAction: {s}")

    def is_final(self) -> bool:
        return self in {
            TestAction.PASS,
            TestAction.BENCH,
            TestAction.FAIL,
            TestAction.SKIP,
        }


class RawTestEvent(TypedDict):
    """Raw output from `go test -json`"""

    Action: str
    Time: Optional[str]
    Package: Optional[str]
    Test: Optional[str]
    Elapsed: Optional[float]
    Output: Optional[str]


class TestEvent:
    __slots__ = "action", "time", "package", "test", "elapsed", "output"

    def __init__(
        self,
        action: TestAction,
        time: Optional[str] = None,
        package: Optional[str] = None,
        test: Optional[str] = None,
        elapsed: Optional[float] = None,
        output: Optional[str] = None,
    ):
        self.action = action
        self.time = time
        self.package = package
        self.test = test
        self.elapsed = elapsed
        self.output = output

    @classmethod
    def _from_raw_json(cls, raw: Dict[str, Any]) -> "TestEvent":
        m = cast(RawTestEvent, raw)
        return TestEvent(
            action=TestAction.from_string(m["Action"]),
            time=m.get("Time"),
            package=m.get("Package"),
            test=m.get("Test"),
            elapsed=m.get("Elapsed"),
            output=m.get("Output"),
        )

    @classmethod
    def from_json(cls, line: AnyStr) -> "TestEvent":
        try:
            return json.loads(line, object_hook=cls._from_raw_json)
        except Exception as e:
            raise ValueError(f"invalid JSON: '{line!s}'") from e

    @property
    def test_name(self) -> Optional[str]:
        return self.test

    def get_time(self) -> str:
        return self.time if self.time else ""

    def get_package(self) -> str:
        return self.package if self.package else ""

    def get_test(self) -> str:
        return self.test if self.test else ""

    def get_elapsed(self) -> float:
        return self.elapsed if self.elapsed else 0

    def get_output(self) -> str:
        return self.output if self.output else ""

    def __repr__(self) -> str:
        args = []
        for attr in self.__slots__:
            v = getattr(self, attr)
            if v is not None:
                args.append(f"{attr}={v!r}")
        return f"{self.__class__.__name__}({', '.join(args)})"


class Failure:
    __slots__ = "filename", "line", "failure", "output", "combined_output"

    def __init__(
        self,
        filename: str,
        line: int,
        failure: Optional[str] = None,
        output: List[str] = [],
        combined_output: Optional[str] = None,
    ):
        self.filename = filename
        self.line = line
        self.failure = failure
        self.output = output
        self.combined_output = combined_output

    def __repr__(self) -> str:
        args = []
        for attr in self.__slots__:
            v = getattr(self, attr)
            if v is not None:
                args.append(f"{attr}={v!r}")
        return f"{self.__class__.__name__}({', '.join(args)})"


# def count_leading_spaces(s: str) -> int:
#     for i, c in enumerate(s):
#         if not c.isspace():
#             return i
#     return -1
#
#
# def trim_prefix(s: str, prefix: str) -> str:
#     return s[len(prefix):] if s.startswith(prefix) else ""


# TODO: move to method
def parse_combined_output(f: Failure) -> str:
    if f.output is None or len(f.output) == 0:
        return ""  # TODO: Optional[str] ???

    # TODO: use range() and steps of 4
    prefix = ""
    for i, c in enumerate(f.output[0]):
        if c != " ":
            # Add 4 spaces since the next lines are indented at the next level
            prefix = f.output[0][:i] + "    "
            break

    combined = [
        (s[len(prefix) :] if s.startswith(prefix) else s).rstrip("\n")
        for s in f.output
    ]
    combined[0] = REPLACE_FILENAME_LINE_RE.sub("", combined[0])

    return "\n".join(combined)


# TODO: rename ???
class Test:
    __slots__ = "name", "package", "status", "failures"

    def __init__(
        self,
        name: str,
        package: str,
        status: TestAction,
        failures: List[Failure] = [],
    ):
        self.name = name
        self.package = package
        self.status = status
        self.failures = failures

    def is_subtest(self) -> bool:
        return "/" in self.name

    @classmethod
    def _final_action(cls, events: List[TestEvent]) -> Optional[TestAction]:
        for e in events:
            if e.action.is_final():
                return e.action
        return None

    @classmethod
    def from_events(cls, events: List[TestEvent]) -> "Test":
        if len(events) == 0:
            raise ValueError("empty events list")

        if any(e.test != events[0].test for e in events):
            raise ValueError("found multiple tests in events list")

        package_name = events[0].get_package()
        # WARN: should we allow this to be empty?
        test_name = events[0].test_name or ""

        final_action = cls._final_action(events)
        if final_action is None:
            raise ValueError(f"no final action for test: {events[0].test}")

        # Remove the first event if it's an update
        if TEST_UPDATE_RE.match(events[0].get_output()) is not None:
            del events[0]

        # WARN WARN WARN
        del events[0]

        failures: List[Failure] = []
        while len(events) > 0:
            e = events.pop(0)
            if e.action is not TestAction.OUTPUT:
                continue
            if not e.output:
                continue

            le = parse_line_error(e.get_output())
            if le is None:
                continue

            failure = Failure(
                filename=le.filename,
                line=le.line,
                failure=le.message,
                output=[e.get_output()],
            )

            # Consume any extra lines that are associated with this failure
            extra = events
            for i, o in enumerate(extra):
                if o.action == TestAction.OUTPUT:
                    out = o.get_output()
                    if (
                        FILENAME_LINE_FAILURE_RE.match(out) is not None
                        or TEST_REPORT_RE.match(out) is not None
                        or TEST_UPDATE_RE.match(out) is not None
                    ):
                        # remove any of the events we just consumed
                        events = events[i:]
                        break
                    else:
                        failure.output.append(o.get_output())

            # while len(events) > 0:
            #     o = events[0]  # Don't pop since we may not consume
            #     if o.action != TestAction.OUTPUT:
            #         continue
            #     out = o.get_output()
            #     if (
            #         FILENAME_LINE_FAILURE_RE.match(out) is not None or
            #         TEST_REPORT_RE.match(out) is not None or
            #         TEST_UPDATE_RE.match(out) is not None
            #     ):
            #         break
            #     failure.output.append(out)
            #     events = events[1:]

            # WARN: implement me
            failure.combined_output = parse_combined_output(failure)
            failures.append(failure)

        return Test(
            name=test_name,
            package=package_name,
            status=final_action,
            failures=failures,
        )


class RawTestOutput:
    __slots__ = "pkgs"

    def __init__(
        self,
        pkgs: Dict[str, Dict[str, List[TestEvent]]] = {},
    ):
        # Package => Test => Events
        self.pkgs = pkgs

    def add_event(self, ev: TestEvent) -> None:
        # WARN: this needs a review
        if ev.package is not None and ev.test is not None:
            if ev.package not in self.pkgs:
                self.pkgs[ev.package] = {ev.test: []}
            if ev.test not in self.pkgs[ev.package]:
                self.pkgs[ev.package][ev.test] = []
            self.pkgs[ev.package][ev.test].append(ev)

    def add_test(self, pkg: str, name: str, events: list[TestEvent]) -> None:
        if pkg not in self.pkgs:
            self.pkgs[pkg] = {}
        self.pkgs[pkg][name] = events.copy()

    def package_names(self) -> KeysView[str]:
        return self.pkgs.keys()

    def filter_by_action(self, action: str) -> "RawTestOutput":
        o = RawTestOutput()
        for pkg, test in self.pkgs.items():
            for name, events in test.items():
                if any(e.action == action for e in events):
                    o.add_test(pkg, name, events)
        return o

    @classmethod
    def from_events(cls, events: List[TestEvent]) -> "RawTestOutput":
        o = RawTestOutput()
        for e in events:
            o.add_event(e)
        return o


def parse_events(test_output: str) -> RawTestOutput:
    out = RawTestOutput()
    for line in test_output.split("\n"):
        if line:
            out.add_event(TestEvent.from_json(line))
    return out


class Package:
    __slots__ = "_tests", "_benchmarks"

    def __init__(
        self,
        tests: Optional[Dict[str, Test]] = None,
        benchmarks: Optional[Dict[str, Test]] = None,
    ):
        self._tests = tests  # Name => Test
        self._benchmarks = benchmarks  # Name => Test

    def test_names(self) -> Optional[KeysView[str]]:
        return self._tests.keys() if self._tests is not None else None

    def failures(self) -> List[Test]:
        if self._tests is not None:
            return [
                t for t in self._tests.values() if t.status is TestAction.FAIL
            ]
        return []


def run_tests() -> None:
    proc = subprocess.run(
        ["go", "test", "-json", "."],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )
    if proc.returncode == 0:
        return  # WARN
    if proc.stderr:
        # WARN this has no context on the error
        proc.check_returncode()
    events: List[TestEvent] = []
    for line in proc.stdout.split("\n"):
        if not line:
            continue
        try:
            events.append(TestEvent.from_json(line))
        except Exception as ex:
            print(f"LINE: {line}")
            raise ex
    print(f"events: {len(events)}")
    for ev in events:
        print(ev)


def run_tests_new() -> List[TestEvent]:
    proc = subprocess.run(
        ["go", "test", "-json", "."],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )
    if proc.stderr:
        # WARN: the string representation of this exception does not
        # include STDERR
        proc.check_returncode()

    return [
        TestEvent.from_json(line) for line in proc.stdout.split("\n") if line
    ]


def match_view(view: sublime.View, selector: str) -> bool:
    syntax = view.syntax()
    if not syntax:
        return False
    return sublime.score_selector(syntax.scope, selector) > 1


class GoTestRun(sublime_plugin.WindowCommand):

    def is_enabled(self) -> bool:
        return self._match_view(self.window.active_view())

    def run(self) -> None:
        view = self.window.active_view()
        if not self._match_view(view):
            logger.warning("not enabled for view: %s", self._view_filename(view))
            return
        self._test_names(view)

    def _view_filename(self, view: Optional[sublime.View]) -> str:
        return view.file_name() or "" if view is not None else ""

    def _match_view(self, view: Optional[sublime.View]) -> bool:
        if view is None or view.is_scratch():
            return False
        return match_view(view, "source.go")

    def _test_names(self, view: sublime.View) -> None:
        dirname = os.path.dirname(self._view_filename(view))
        if not os.path.isdir(dirname):
            logger.warning("directory does not exist: %s", dirname)
            return

        def callback(proc: Optional[CompletedProcess], exc: Optional[Exception]) -> None:
            if exc is not None:
                logger.exception(f"failed to list tests: {exc}", exc_info=exc)
            elif proc is None:
                logger.error("both CompletedProcess and Exception are None")
            else:
                print("### Stdout:")
                print(proc.stdout)
                print("###")

        proc = AsyncProcess(
            cmd=["go", "test", "-list", "."],
            callback=callback,
            cwd=dirname,
        )
        sublime.set_timeout_async(proc.run, 1)

    def _run_tests(self, view: sublime.View) -> None:
        # TODO: save file before running?
        pass


def plugin_loaded() -> None:
    pass
