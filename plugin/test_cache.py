import os
import hashlib
import subprocess
from tempfile import NamedTemporaryFile
from collections import OrderedDict
from re import sub
from time import time
from typing import List, Optional, Set
from typing import NamedTuple

# WARN DEV ONLY
try:
    from sublime import packages_path
except ModuleNotFoundError:

    def packages_path() -> str:
        return os.path.abspath(os.path.join(os.path.dirname(__file__), "tmp"))


_mswindows = os.name == "nt"

GO_CMD_DIR = os.path.abspath(os.path.join(__file__, "../../cmd"))
GO_TEST_UTIL_DIR = os.path.join(GO_CMD_DIR, "gotest-util")


class FileEntry(NamedTuple):
    name: str
    mtime_ns: int
    size: int
    # hash: int


def hash_file(filename: str) -> int:
    with open(filename) as f:
        return hash(f.read())


def cache_directory(dirname: str) -> Set[FileEntry]:
    tests = set()
    with os.scandir(dirname) as it:
        for entry in it:
            if entry.name.endswith("_test.go") and entry.is_file():
                try:
                    st = entry.stat()
                    tests.add(FileEntry(entry.name, st.st_mtime_ns, st.st_size))
                except FileNotFoundError:
                    pass
    return tests


class TestNameCache:
    """docstring for TestNameCache"""

    def __init__(self, maxsize: int = 64):
        self.maxsize = maxsize
        self._cache: OrderedDict[str, str] = OrderedDict()

    # def __delitem__(self, key: str) -> None:
    #     OrderedDict.__delitem__(self, key)


def hash_go_files() -> str:
    go_files = []
    for root, dirs, files in os.walk(GO_CMD_DIR):
        for file in files:
            if file.endswith(".go") or file == "go.mod" or file == "go.sum":
                go_files.append(os.path.join(root, file))
        if "vendor" in dirs:
            del dirs[dirs.index("vendor")]

    m = hashlib.md5()
    for file in sorted(go_files):
        try:
            with open(file) as f:
                m.update(file.encode("utf-8"))
                m.update(f.read().encode("utf-8"))
        except FileNotFoundError:
            pass

    return m.hexdigest()


def check_output(
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
        startupinfo=startupinfo,
        preexec_fn=preexec_fn,
        encoding="utf-8",
    )
    return output.rstrip() if output and rstrip else output


def go_version(goexe: str = "go") -> str:
    version = check_output([goexe, "version"])
    for prefix in ["go version devel ", "go version "]:
        if version.startswith(prefix):
            version = version[len(prefix):]
    return version.split(" ")[0]


def get_expected_version(goexe: str = "go") -> str:
    return go_version(goexe) + "-" + hash_go_files()[:8]


def gotest_util_exe() -> str:
    if not _mswindows:
        testexe = "gotest-util"
    else:
        testexe = "gotest-util.exe"
    return os.path.join(packages_path(), "User", "GoTest", "bin", testexe)


# TODO: run in another thread and set a variable when complete
def install_gotest_util(goexe: str = "go") -> None:
    expected_version = get_expected_version()
    testexe = gotest_util_exe()
    if os.path.exists(testexe):
        if check_output([testexe, "version"]) == expected_version:
            return

    # Make sure the bin dir exists
    os.makedirs(os.path.dirname(testexe), exist_ok=True)

    # TODO: log that we are updating / building

    temp = NamedTemporaryFile(suffix=".exe", prefix=testexe + "-").name
    try:
        ldflags = f"-ldflags=-X main.version={expected_version}"
        check_output(
            [goexe, "build", ldflags, "-o", temp],
            cwd=GO_TEST_UTIL_DIR,
        )
        current_version = check_output([temp, "version"])
        if current_version != expected_version:
            raise RuntimeError(
                (f"gotest-util: expected version: {expected_version} " +
                 f"got version: {current_version}"),
            )

        # Overwrite the old exe
        os.rename(temp, testexe)
    finally:
        if os.path.exists(temp):
            os.remove(temp)


if __name__ == "__main__":
    install_gotest_util()
    pass
