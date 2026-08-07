"""Subprocess lifecycle primitives used by the exporter pipeline.

The pipeline owns orchestration and presentation; this module owns spawning,
stream selection, process-group termination, and cleanup of child resources.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess


FAIL_TAIL_LINES = 40


def reader(stream, queue):
    """Read child output, splitting both newline and carriage-return updates."""
    buf = b""
    try:
        while True:
            chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(b"[\r\n]", buf)
            buf = parts.pop()
            for part in parts:
                queue.put(part.decode("utf-8", "replace"))
    except Exception:
        pass
    finally:
        if buf:
            queue.put(buf.decode("utf-8", "replace"))
        queue.put(None)


class Child:
    """A configured subprocess with process-group and output-handle lifecycle."""

    def __init__(self, cmd, cwd, env=None, stdout_file=None):
        self.cmd = cmd
        self.cwd = cwd
        self.env = env
        self.stdout_file = stdout_file
        self.proc = None
        self._out_fh = None

    def start(self):
        """Spawn the process and return its progress stream."""
        env = dict(os.environ)
        if self.env:
            env.update(self.env)
        self._out_fh = open(self.stdout_file, "wb") if self.stdout_file else None
        try:
            self.proc = subprocess.Popen(
                self.cmd,
                cwd=str(self.cwd),
                stdout=(self._out_fh if self._out_fh else subprocess.PIPE),
                stderr=(subprocess.PIPE if self._out_fh else subprocess.STDOUT),
                env=env,
                start_new_session=True,
            )
        except BaseException:
            self.close()
            raise
        return self.proc.stderr if self._out_fh else self.proc.stdout

    def kill_group(self):
        """Terminate the child process group, escalating to SIGKILL if needed."""
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except Exception:
                pass

    def close(self):
        if self._out_fh:
            self._out_fh.close()
            self._out_fh = None


# Compatibility aliases retain the old private seam while callers migrate.
_reader = reader

