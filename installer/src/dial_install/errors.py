"""Exceptions carrying customer-readable messages."""

from __future__ import annotations


class InstallerError(Exception):
    """A problem the customer can act on. `hint` says how."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class CommandError(InstallerError):
    """An external command (aws, kubectl, helm) failed."""

    def __init__(self, argv: list[str], returncode: int, stderr: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr.strip()
        head = " ".join(argv[:3])
        super().__init__(f"command failed ({returncode}): {head} ...\n{self.stderr}")
