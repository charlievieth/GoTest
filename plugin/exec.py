import os
import signal
import subprocess
import threading
import time

from typing import Callable
from typing import Dict
from typing import List
from typing import Optional

_mswindows = os.name == "nt"

# TODO: consider using a separate error handler
#
# Called with stdout, stderr, Optional[error message]
# Callback = Callable[[Optional[str], Optional[str], Optional[Exception]], None]
#
Callback = Callable[[Optional[subprocess.CompletedProcess], Optional[Exception]], None]


class AsyncProcess:
    def __init__(
        self,
        cmd: List[str],
        callback: Callback,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        encoding: str = "utf-8",
    ) -> None:

        if not cmd:
            raise ValueError("cmd is required")

        if not callback:
            raise ValueError("callback is required")

        self.killed = False
        self.callback = callback

        proc_env = os.environ.copy()
        if env is not None:
            proc_env.update(env)
        proc_env = {k: os.path.expanduser(v) for k, v in proc_env.items()}

        # Hide the console window on Windows
        startupinfo = None
        if _mswindows:
            startupinfo = subprocess.STARTUPINFO()  # type: ignore
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore

        if not _mswindows:
            preexec_fn = None
        else:
            preexec_fn = os.setsid

        self.proc = subprocess.Popen(
            cmd,
            bufsize=0,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            startupinfo=startupinfo,
            cwd=cwd,
            env=proc_env,
            preexec_fn=preexec_fn,
            encoding=encoding,
        )

        self.proc_thread = threading.Thread(
            target=self._communicate,
            # args=(timeout,),
            kwargs={"timeout": timeout},
        )

    def kill(self) -> None:
        if not self.killed:
            self.killed = True
            if _mswindows:
                # TODO: not this will not kill child procs
                self.proc.kill()
            else:
                try:
                    # WARN: this does not appear to work
                    os.killpg(self.proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                finally:
                    self.proc.terminate()

    # TODO: rename to "_communicate"
    def _run(self, timeout: Optional[float] = None) -> subprocess.CompletedProcess:
        def _kill() -> None:
            if not self.killed:
                self.killed = True
                self.proc.kill()

        with self.proc:
            try:
                stdout, stderr = self.proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                _kill()
                if _mswindows:
                    # Windows accumulates the output in a single blocking
                    # read() call run on child threads, with the timeout
                    # being done in a join() on those threads. communicate()
                    # _after_ kill() is required to collect that and add it
                    # to the exception.
                    exc.stdout, exc.stderr = self.proc.communicate()
                else:
                    # POSIX _communicate already populated the output so
                    # far into the TimeoutExpired exception.
                    self.proc.wait()
                raise
            except:
                _kill()
                raise

        retcode = self.proc.poll() or 0  # TODO: mypy flags this as "int | None"
        return subprocess.CompletedProcess(
            args=self.proc.args,
            returncode=retcode,
            stdout=stdout,
            stderr=stderr,
        )

    # TODO: rename to "_run"
    def _communicate(self, timeout: Optional[float] = None) -> None:
        try:
            self.callback(self._run(timeout), None)
        except Exception as exc:
            self.callback(None, exc)

    def run(self) -> None:
        self.proc_thread.start()

    def join(self) -> None:
        self.proc_thread.join()


if __name__ == "__main__":

    def callback(
        proc: Optional[subprocess.CompletedProcess],
        exc: Optional[Exception],
    ) -> None:
        if exc is not None:
            raise exc
        if proc and proc.stdout:
            print("### STDOUT:")
            print(proc.stdout)
            print("###")
        if proc and proc.stderr:
            print("### STDERR:")
            print(proc.stderr)
            print("###")

    # ap = AsyncProcess(["go", "version"], callback)
    # ap = AsyncProcess(["sleep", "10"], callback, timeout=1)
    ap = AsyncProcess(["does-not-exist", "10"], callback, timeout=1)
    print("START")
    ap.run()
    # time.sleep(0.20)
    # ap.kill()
    ap.join()
