# WARN WARN WARN WARN
from pprint import pprint

import json
import os
import re
import subprocess
import functools
import html

from enum import Enum
from typing import Any, Tuple
from typing import AnyStr
from typing import Dict
from typing import KeysView
from typing import List
from typing import NamedTuple
from typing import Optional
from typing import TypedDict
from typing import Union
from typing import cast
from subprocess import CompletedProcess

# from typing import NamedTuple
# from typing import TypedDict

from .plugin.logger import get_logger
from .plugin.exec import AsyncProcess
from .plugin.exec import Callback
from .plugin.testutil import list_tests
from .plugin.testutil import test_env
from .plugin.testutil import view_overlay
from .plugin.testutil import ListResponse
from .plugin.testutil import FuncDefinition
from .plugin.utils import view_file_name

import sublime
import sublime_plugin

# WARN WARN WARN
import Default.exec


logger = get_logger()

# REPLACE_FILENAME_LINE_RE = re.compile(r"(^\s*\w+\.go:\d+: )")
REPLACE_FILENAME_LINE_RE = re.compile(r"^\s+.*?\.go:\d+: ")
FILENAME_LINE_FAILURE_RE = re.compile(r"^[    ]{1,}(.+?\.go):(\d+): (.*)")
TEST_REPORT_RE = re.compile(r"^[    ]{0,}--- (?:PASS|FAIL|SKIP|BENCH): .+")
# TEST_REPORT_RE = re.compile(r"^[    ]{0,}--- (PASS|FAIL|SKIP|BENCH): \w+")
TEST_UPDATE_RE = re.compile(r"^=== (?:RUN|PAUSE|CONT)\s+")

# PROGRESS_SPINNER_CHARS = ["◐", "◓", "◑", "◒"]
PROGRESS_SPINNER_CHARS = "◓◑◒◐"
# MSG_CHARS_COLOR_SUBLIME = u'⣾⣽⣻⢿⡿⣟⣯⣷'


PREVIEW_PANE_CSS = """
    .diagnostics {padding: 0.5em}
    .diagnostics a {color: var(--bluish)}
    .diagnostics.error {background-color: color(var(--redish) alpha(0.25))}
    .diagnostics.warning {background-color: color(var(--yellowish) alpha(0.25))}
    .diagnostics.info {background-color: color(var(--bluish) alpha(0.25))}
    .diagnostics.hint {background-color: color(var(--bluish) alpha(0.25))}
    """


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

    def to_dict(self) -> Dict[str, Any]:
        m = {}
        for attr in self.__slots__:
            v = getattr(self, attr)
            if v is not None:
                m[attr] = v
        return m

    def short_error_msg(self) -> str:
        if self.combined_output:
            if "\n" not in self.combined_output:
                return self.combined_output.strip()
            else:
                # find the first non-empty line
                for line in self.combined_output.split("\n"):
                    line = line.strip()
                    if line:
                        return line
        return ""


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
        (s[len(prefix) :] if s.startswith(prefix) else s).rstrip("\n") for s in f.output
    ]
    combined[0] = REPLACE_FILENAME_LINE_RE.sub("", combined[0])

    return "\n".join(combined)


# TODO: rename ???
class Test:
    __slots__ = "_name", "package", "status", "failures"

    def __init__(
        self,
        name: str,
        package: str,
        status: TestAction,
        failures: List[Failure] = [],
    ):
        self._name = name
        self.package = package
        self.status = status
        self.failures = failures
        # self.failures = sorted(failures, key=lambda ff: ff.line)

    @property
    def full_name(self) -> str:
        return self._name

    @property
    def is_subtest(self) -> bool:
        return "/" in self._name

    @property
    def name(self) -> str:
        if self.is_subtest:
            return self._name.split("/", 1)[0]
        else:
            return self._name

    def __repr__(self) -> str:
        args = []
        for attr in self.__slots__:
            v = getattr(self, attr)
            if v is not None:
                args.append(f"{attr}={v!r}")
        return f"{self.__class__.__name__}({', '.join(args)})"

    # WARN: debug only
    def to_dict(self) -> Dict[str, Any]:
        if self.failures:
            failures = [f.to_dict() for f in self.failures]
        else:
            failures = None
        return {
            "name": self.name,
            "full_name": self.full_name,
            "package": self.package,
            "status": str(self.status),
            "failures": failures,
        }

    # WARN: debug only
    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

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

    @classmethod
    def from_test_output(
        cls,
        output: str,
        all_tests: bool = False,
    ) -> "List[Test]":
        # dec = json.JSONDecoder(object_hook=TestEvent._from_raw_json)
        # events = []
        # while output:
        #     e, i = dec.raw_decode(output)
        #     output = output[i:]
        #     events.append(e)
        o = RawTestOutput.from_events(
            [TestEvent.from_json(line) for line in output.split("\n") if line]
        )
        tests: List[Test] = []
        for raw_tests in o.pkgs.values():
            for events in raw_tests.values():
                tt = Test.from_events(events)
                if all_tests or (tt.status is TestAction.FAIL and tt.failures):
                    tests.append(tt)
        return tests


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

    def add_test(self, pkg: str, name: str, events: List[TestEvent]) -> None:
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
            return [t for t in self._tests.values() if t.status is TestAction.FAIL]
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

    return [TestEvent.from_json(line) for line in proc.stdout.split("\n") if line]


def match_view(view: Optional[sublime.View]) -> bool:
    if view and not view.is_scratch():
        syntax = view.syntax()
        if syntax:
            # Every part of a x.y.z scope seems to contribute 8.
            # An empty selector result in a score of 1.
            # A non-matching non-empty selector results in a score of 0.
            #
            # We want to match at least one part of an x.y.z, and we don't
            # want to match on empty selectors.
            return sublime.score_selector(syntax.scope, "source.go") >= 8
    return False


def view_window(view: Optional[sublime.View]) -> Optional[sublime.Window]:
    if view and view.is_valid():
        window = view.window()
        if window and window.is_valid():
            return window
    return None


def view_filename(view: Optional[sublime.View]) -> str:
    if view is None:
        return ""
    return view.file_name() or ""


def view_src(view: sublime.View) -> str:
    """Returns the string source of the Sublime view."""
    return view.substr(sublime.Region(0, view.size())) if view else ""


class GoTestRun(sublime_plugin.WindowCommand):
    STATUS_KEY = "000_GoTest"  # TODO: use request_id for this
    ALL_TESTS = "All Tests"
    SHORT_TESTS = "Short Tests"
    ANNOTATION_KEY = "GoTest"

    def is_enabled(self) -> bool:
        return match_view(self.window.active_view())

    def run(self) -> None:
        view = self.window.active_view()
        if view is None:
            return

        if not match_view(view):
            logger.warning("not enabled for view: %s", view_file_name(view))
            return

        self.package_test_names(view)

    # TODO: set status for all views in the window
    # TODO: use a spinner to show that it's running
    def clear_status_callback(
        self,
        view: sublime.View,
        cb: Callback,
        async_delay: Optional[int] = None,
    ) -> Callback:
        def outer(proc: Optional[CompletedProcess], exc: Optional[Exception]) -> None:
            try:
                cb(proc, exc)
            finally:
                if not view.get_status(self.STATUS_KEY):
                    return
                if async_delay is not None:
                    sublime.set_timeout_async(
                        lambda: view.erase_status(self.STATUS_KEY),
                        async_delay,
                    )
                else:
                    view.erase_status(self.STATUS_KEY)  # TODO: use request_id for this

        return outer

    def quick_panel_items(self, tests: ListResponse) -> List[sublime.QuickPanelItem]:
        if not tests or not tests.tests:
            return []

        items = [
            sublime.QuickPanelItem(
                trigger=self.ALL_TESTS,
                details="run all tests",
                # annotation="ANNOTATION: 1",
                kind=sublime.KIND_VARIABLE,
            ),
            sublime.QuickPanelItem(
                trigger=self.SHORT_TESTS,
                details="run short tests",
                # annotation="",
                kind=sublime.KIND_VARIABLE,
            ),
        ]
        for test in tests.tests:
            items.append(
                sublime.QuickPanelItem(
                    trigger=test.name,
                    details=os.path.basename(test.filename),
                    # TODO: what should we use for the annotation ???
                    # annotation=loc.syntax if loc else "",
                    kind=sublime.KIND_FUNCTION,
                )
            )
        return items

    def jump_to_location(
        self,
        view: sublime.View,
        filename: str,
        line: int,
        transient: bool = False,
    ) -> Optional[sublime.View]:
        window = view_window(view)
        if window is None:
            return None
        flags = sublime.ENCODED_POSITION
        if transient:
            # flags |= sublime.TRANSIENT
            flags |= sublime.FORCE_GROUP
            flags |= sublime.REPLACE_MRU | sublime.SEMI_TRANSIENT
        else:
            view.run_command(
                "add_jump_record", {"selection": [(r.a, r.b) for r in view.sel()]}
            )

        group = window.active_group()
        return window.open_file(f"{filename}:{line}", flags=flags, group=group)

    def _same_file(self, p1: str, p2: str) -> bool:
        return p1 == p2 or os.path.basename(p1) == os.path.basename(p2)

    class _LineError:
        __slots__ = "line", "error"

        def __init__(self, line: int, error: str) -> None:
            self.line = line
            self.error = error

    def _update_view_annotations(self, view: sublime.View, test_failures: List[Test]) -> None:
        # WARN: DEV ONLY
        logger.warning("update_view_annotations")

        stylesheet = '''
            <style>
                #annotation-error {
                    background-color: color(var(--background) blend(#fff 95%));
                }
                html.dark #annotation-error {
                    background-color: color(var(--background) blend(#fff 95%));
                }
                html.light #annotation-error {
                    background-color: color(var(--background) blend(#000 85%));
                }
                a {
                    text-decoration: inherit;
                }
            </style>
        '''

        filename = view_filename(view)
        if not filename:
            return
        failures: List[Failure] = []
        for t in test_failures:
            for ff in t.failures or []:
                if self._same_file(filename, ff.filename):
                    failures.append(ff)

        failures = sorted(failures, key=lambda ff: ff.line)

        selection_set: List[sublime.Region] = []
        content_set: List[str] = []
        line_err_set: List["GoTestRun._LineError"] = []
        for ff in failures:
            if not ff.combined_output:
                continue
            # WARN: need to fix column
            pt = view.text_point(ff.line - 1, 0)
            if line_err_set and ff.line == line_err_set[-1].line:
                # WARN: make sure this works
                line_err_set[-1].error += "<br>" + html.escape(
                    ff.combined_output, quote=False,
                )
            else:
                # pt_b = pt + 1
                r = view.expand_by_class(pt, sublime.CLASS_WORD_START | sublime.CLASS_LINE_END)
                pt_a = r.b
                r = view.expand_by_class(pt, sublime.CLASS_LINE_END)
                pt_b = r.b

                # if view.classify(pt) & sublime.CLASS_WORD_START:
                #     pt_b = view.find_by_class(
                #         pt, forward=True, classes=(sublime.CLASS_WORD_END)
                #     )
                # if pt_b <= pt:
                #     pt_b = pt + 1
                # selection_set.append(sublime.Region(pt, pt_b))
                selection_set.append(sublime.Region(pt_a, pt_b))

                # line_err_set.append([ff.line, html.escape(ff.combined_output, quote=False)])
                line_err_set.append(
                    self._LineError(ff.line, html.escape(ff.combined_output, quote=False),
                ))

        logger.warning("### line_err_set:")
        pprint(line_err_set)
        logger.warning("###")
        for le in line_err_set:
            content_set.append(
                "<body>"
                + stylesheet
                + '<div class="error" id=annotation-error>'
                + '<span class="content">'
                + le.error
                + "</span></div>"
                + "</body>"
            )

        view.add_regions(
            self.ANNOTATION_KEY,
            selection_set,
            scope="invalid",
            annotations=content_set,
            icon="dot",
            flags=(
                sublime.DRAW_SQUIGGLY_UNDERLINE
                | sublime.DRAW_NO_FILL
                | sublime.DRAW_NO_OUTLINE
            ),
            on_close=lambda: self._hide_annotations(test_failures),
        )

    def _hide_annotations(self, test_failures: List[Test]) -> None:
        if not test_failures:
            return

        files_with_errs = set()
        for test in test_failures:
            for ff in test.failures or []:
                files_with_errs.add(ff.filename)

        for window in sublime.windows():
            for file in files_with_errs:
                view = window.find_open_file(file)
                if view:
                    view.erase_regions(self.ANNOTATION_KEY)
                    view.hide_popup()

        view = sublime.active_window().active_view()
        if view:
            view.erase_regions("exec")
            view.hide_popup()

        self.errs_by_file = {}
        self.annotation_sets_by_buffer = {}
        self.show_errors_inline = False

    def handle_test_output(
        self,
        view: sublime.View,
        test_funcs: ListResponse,
        proc: Optional[subprocess.CompletedProcess] = None,
        exc: Optional[Exception] = None,
    ) -> None:
        view.erase_status(self.STATUS_KEY)  # Clear the "Running tests" status
        if exc:
            # TODO: handle this and highlint errors
            if isinstance(exc, subprocess.CalledProcessError):
                sublime.error_message(f"Exception: {exc}\n###\n{exc.stderr}\n###")
            else:
                sublime.error_message(f"Exception: {exc}")
            return
        if not proc:
            raise ValueError("either proc or exc should be supplied")

        failures = Test.from_test_output(proc.stdout)

        # WARN: dev only
        if any(not ff.failures for ff in failures):
            logger.error(
                "tests with no failures: %s",
                [ff.full_name for ff in failures if not ff.failures],
            )
            failures = [ff for ff in failures if ff.failures]

        if not failures:
            # WARN: signal that all tests passed
            view.set_status(self.STATUS_KEY, "All tests passed")
        else:
            view.set_status(self.STATUS_KEY, f"Found {len(failures)} test failures")

            # TODO: use the index for this
            test_files = {tt.name: tt for tt in test_funcs.tests or []}
            items: List[sublime.QuickPanelItem] = [
                sublime.QuickPanelItem(
                    trigger=f"Found {len(failures)} test failures",
                    kind=sublime.KIND_VARIABLE,
                )
            ]
            for test in failures:
                failure = test.failures[0]
                items.append(
                    sublime.QuickPanelItem(
                        trigger=test.full_name,
                        details=f"<code>{failure.output[0]}</code>",
                        annotation=os.path.basename(failure.filename),
                        kind=sublime.KIND_FUNCTION,
                    )
                )

            highlighted_view: Optional[sublime.View] = None

            def _on_select(index: int, transient: bool) -> None:
                # WARN WARN WARN
                logger.warning(f"on_select: index: {index} transient: {transient}")

                nonlocal highlighted_view
                # Index 0 is the test message
                if index > 0:
                    test = failures[index - 1]
                    if test.failures:
                        filename = test.failures[0].filename
                        line = test.failures[0].line
                    else:
                        logger.error(f"WTF: {test!s}")  # WARN: remove
                        func = test_files[test.name]
                        filename = func.filename
                        line = func.line
                    # func = test_files[test.name]
                    # new_view = self.jump_to_location(view, func.filename, func.line, transient)
                    new_view = self.jump_to_location(view, filename, line, transient)
                    if new_view:
                        highlighted_view = new_view
                        self._update_view_annotations(view, failures)
                else:
                    window = view_window(view)
                    if window is not None:
                        window.focus_view(view)
                    # on_select is called with -1 when the panel is closed
                    if not transient and highlighted_view:
                        sheet = highlighted_view.sheet()
                        if sheet and sheet.is_semi_transient():
                            highlighted_view.close()

            self.window.show_quick_panel(
                items=items,
                on_select=lambda index: _on_select(index, transient=False),
                selected_index=-1,
                on_highlight=lambda index: _on_select(index, transient=True),
                placeholder="Test failures",
                flags=sublime.KEEP_OPEN_ON_FOCUS_LOST,
            )
            # for tt in tests:
            #     pass
        sublime.set_timeout_async(lambda: view.erase_status(self.STATUS_KEY), 5000)

    def handle_test_output_callback(
        self,
        view: sublime.View,
        test_funcs: ListResponse,
    ) -> Callback:
        def callback(
            proc: Optional[subprocess.CompletedProcess] = None,
            exc: Optional[Exception] = None,
        ) -> None:
            self.handle_test_output(view, test_funcs, proc, exc)

        return callback

    def package_test_names(self, view: sublime.View) -> None:
        filename = view_file_name(view)
        dirname = os.path.dirname(filename)
        if not os.path.isdir(dirname):
            # TODO: notify user
            logger.warning("directory does not exist: %s", dirname)
            sublime.error_message(
                "Error: directory does not exist: {}".format(dirname),
            )
            return

        # TODO: handle exceptions
        tests = list_tests(filename, view_overlay([view]))
        if tests is None or not tests.tests:
            # TODO: log that there are no tests to run
            sublime.error_message(
                "Warn: no tests for: {}".format(os.path.basename(filename)),
            )
            return

        items = self.quick_panel_items(tests)
        if not items:
            return  # This should never happen

        def on_select(index: int) -> None:
            # TODO: run the tests async instead of setting a var
            if index < 0:
                return

            cmd = ["go", "test", "-json"]
            it = items[index]
            if it.trigger == self.ALL_TESTS:
                pass
            elif it.trigger == self.SHORT_TESTS:
                cmd.append("-short")
            else:
                cmd += ["-run", f"^{it.trigger}$"]

            sublime.set_timeout_async(
                AsyncProcess(
                    cmd=cmd,
                    callback=self.handle_test_output_callback(view, tests),
                    cwd=dirname,
                    env=tests.environ(),
                ).run
            )
            view.set_status(self.STATUS_KEY, "Running tests...")

        self.window.show_quick_panel(
            items=items,
            on_select=on_select,
            placeholder="All Tests",
        )

        return

    def _run_tests(self, view: sublime.View) -> None:
        # TODO: save file before running?
        pass


# WARN WARN WARN
class RunGoTestsCommand(Default.exec.ExecCommand):
    # Taken from "C++/C Single File.sublime-build"
    # TODO: the column is wrong
    FILE_REGEX = r"^(?:\s+)(..[^:]*):([0-9]+):?([0-9]+)?:? (.*)$"
    # FILE_REGEX = r"^(?:\s+)(..[^:]*):([0-9]+): (.*)$"

    # FILE_REGEX = r"^(?:    )+(\w+\.go)(?=:\d+: )"
    # LINE_REGEX = r"^(?:    )+(?:\w+\.go:)(\d+)(?=: )"

    def run(self, **kwargs) -> None:
        view = self.window.active_view()
        if not view:
            return
        file_name = view.file_name()
        if not file_name:
            return

        # Determine the working directory
        working_dir = kwargs.get("working_dir", None)
        if not working_dir:
            working_dir = os.path.dirname(file_name)

        # Determine the environment variables
        env = os.environ.copy()
        custom_env = kwargs.get("env", None)
        if custom_env:
            env.update(custom_env)

        # Delegate to the super class
        super().run(
            cmd=["go", "test"],
            file_regex=self.FILE_REGEX,
            # line_regex=self.LINE_REGEX,
            working_dir=working_dir,
            env=env,
        )

        def print_errors() -> None:
            print(f"errs_by_file: {len(self.errs_by_file)}")
            pprint(self.errs_by_file)

        sublime.set_timeout_async(print_errors, 3000)


def plugin_loaded() -> None:
    # TODO: install gotest-util if it does not exist since this should
    # be called right after install and we should have network access
    # then.
    #
    # print(f"packages_path: {sublime.packages_path()}")
    pass


# try:
#     proc = AsyncProcess(
#         cmd=["go", "test", "-list", "."],
#         callback=self.clear_status_callback(view, self.test_names_callback),
#         cwd=dirname,
#     )
#     view.set_status(self.STATUS_KEY, "Fetching go test names")
#     # TODO: does this need to be async since we use a thread?
#     sublime.set_timeout_async(proc.run, 1)
# except FileNotFoundError as exc:
#     sublime.error_message("Error: Go is not installed: {}".format(exc))
# except Exception as exc:
#     logger.exception("failed to fetch test names")
#     sublime.error_message("Error: fetching test names: {}".format(exc))
