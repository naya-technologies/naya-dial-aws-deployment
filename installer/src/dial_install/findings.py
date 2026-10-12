"""Check results and their console rendering."""

from __future__ import annotations

from dataclasses import dataclass

OK, WARN, ACTION, FAIL = "ok", "warn", "action", "fail"

_TAGS = {OK: "[ OK ]", WARN: "[WARN]", ACTION: "[TODO]", FAIL: "[FAIL]"}


@dataclass(frozen=True)
class Finding:
    level: str
    subject: str
    message: str
    fix: str = ""


def ok(subject: str, message: str) -> Finding:
    return Finding(OK, subject, message)


def warn(subject: str, message: str, fix: str = "") -> Finding:
    return Finding(WARN, subject, message, fix)


def action(subject: str, message: str, fix: str = "") -> Finding:
    return Finding(ACTION, subject, message, fix)


def fail(subject: str, message: str, fix: str = "") -> Finding:
    return Finding(FAIL, subject, message, fix)


def render(finding: Finding) -> str:
    line = f"{_TAGS[finding.level]} {finding.subject}: {finding.message}"
    if finding.fix:
        indented = "\n".join("         " + part for part in finding.fix.splitlines())
        line += "\n" + indented
    return line


def blocking(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.level in (FAIL, ACTION)]
