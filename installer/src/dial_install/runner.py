"""The only place that starts external processes.

Every call is an argv list (never a shell string). Calls that change something
are marked `write=True`; with --dry-run they are printed instead of executed.
Tests replace `Runner` with `FakeRunner`, so the whole pipeline is testable
without AWS credentials.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from .errors import CommandError


# Network hiccups worth another try. Only read-only commands are retried; a repeated write could do harm.
_TRANSIENT = re.compile(r"connection reset|connection refused|timed out|timeout|temporary failure|"
                        r"unexpected EOF|TLS handshake|i/o timeout|try again|503|500 Internal|RequestLimitExceeded|Throttl", re.I)
_RETRY_DELAYS = (3, 10, 30)


@dataclass
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Runner:
    def __init__(self, dry_run: bool = False, echo: Callable[[str], None] = print) -> None:
        self.dry_run = dry_run
        self._echo = echo

    def run(
        self,
        argv: list[str],
        *,
        write: bool = False,
        check: bool = True,
        input_text: str | None = None,
        stream: bool = False,
        env: dict[str, str] | None = None,
    ) -> Result:
        """Run a command. `stream=True` shows its output live (for slow commands) while still capturing it.
        `env` adds variables to the child's environment (never put secrets on the command line)."""
        if write and self.dry_run:
            self._echo("  [dry-run] " + _display(argv))
            return Result(0)
        try:
            result = self._stream(argv, input_text, env) if stream else self._capture(argv, input_text, env)
            for delay in _RETRY_DELAYS if not write and not stream else ():
                if result.returncode == 0 or not _TRANSIENT.search(result.stderr or ""):
                    break
                self._echo(f"  Network problem, trying again in {delay}s: {_display(argv)[:80]}")
                time.sleep(delay)
                result = self._capture(argv, input_text, env)
        except FileNotFoundError:
            raise CommandError(argv, 127, f"{argv[0]}: command not found") from None
        if check and result.returncode != 0:
            raise CommandError(argv, result.returncode, result.stderr or result.stdout)
        return result


    @staticmethod
    def _capture(argv: list[str], input_text: str | None, env: dict[str, str] | None) -> Result:
        proc = subprocess.run(argv, input=input_text, capture_output=True, text=True, check=False, env=_environment(env))
        return Result(proc.returncode, proc.stdout, proc.stderr)

    def _stream(self, argv: list[str], input_text: str | None, env: dict[str, str] | None) -> Result:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=_environment(env),
        )
        if input_text is not None:
            proc.stdin.write(input_text)
            proc.stdin.close()
        lines = []
        for line in proc.stdout:
            self._echo("    " + line.rstrip())
            lines.append(line)
        proc.wait()
        output = "".join(lines)
        return Result(proc.returncode, output, output if proc.returncode else "")


def _environment(extra: dict[str, str] | None) -> dict[str, str] | None:
    return {**os.environ, **extra} if extra else None


def _display(argv: list[str]) -> str:
    return " ".join(_quote(part) for part in argv)


def _quote(part: str) -> str:
    return part if part and all(c.isalnum() or c in "-_./=:@,+" for c in part) else repr(part)


class FakeRunner(Runner):
    """Test double. `responses` maps a command prefix to a Result, or to a function(argv) -> Result.
    The longest matching prefix wins; unmatched commands succeed with empty output."""

    def __init__(self, responses: dict | None = None, dry_run: bool = False) -> None:
        super().__init__(dry_run=dry_run, echo=lambda _msg: None)
        self.responses = {tuple(k): v for k, v in (responses or {}).items()}
        self.calls: list[list[str]] = []
        self.writes: list[list[str]] = []        # every call that asked for write=True
        self.envs: list[dict[str, str]] = []     # extra environment of each call, parallel to `calls`

    def run(self, argv, *, write=False, check=True, input_text=None, stream=False, env=None):  # type: ignore[override]
        self.calls.append(list(argv))
        self.envs.append(dict(env or {}))
        if write:
            self.writes.append(list(argv))
            if self.dry_run:
                return Result(0)
        best = None
        best_len = -1
        for prefix, result in self.responses.items():
            if tuple(argv[: len(prefix)]) == prefix and len(prefix) > best_len:
                best, best_len = result, len(prefix)
        if callable(best):                         # a response may be computed from the command
            best = best(argv)
        result = best or Result(0, "")
        if check and result.returncode != 0:
            raise CommandError(argv, result.returncode, result.stderr or result.stdout)
        return result

    def called(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if tuple(c[: len(prefix)]) == prefix]
