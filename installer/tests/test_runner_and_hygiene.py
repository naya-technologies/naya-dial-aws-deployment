import ast
import re
import sys
import unittest

from helpers import Base, ROOT
from dial_install.errors import CommandError
from dial_install.runner import Runner


class RetryTests(Base):
    """A network hiccup in a read-only command is retried; a failing write never is."""

    def flaky(self, failures):
        from unittest import mock
        from dial_install import runner as runner_mod
        from dial_install.runner import Result
        calls = []
        results = [Result(1, "", "read tcp: connection reset by peer")] * failures + [Result(0, "ok")]
        instance = Runner(echo=lambda _l: None)

        def capture(argv, input_text, env):
            calls.append(argv)
            return results[min(len(calls) - 1, len(results) - 1)]
        return instance, calls, mock.patch.object(Runner, "_capture", staticmethod(capture)), mock.patch.object(runner_mod.time, "sleep")

    def test_read_is_retried_until_it_works(self):
        instance, calls, capture, sleep = self.flaky(2)
        with capture, sleep:
            self.assertEqual(instance.run(["helm", "show", "values", "x"]).stdout, "ok")
        self.assertEqual(len(calls), 3)

    def test_write_is_never_retried(self):
        from dial_install.errors import CommandError
        instance, calls, capture, sleep = self.flaky(1)
        with capture, sleep, self.assertRaises(CommandError):
            instance.run(["kubectl", "apply"], write=True)
        self.assertEqual(len(calls), 1)

    def test_other_errors_are_not_retried(self):
        from dial_install.errors import CommandError
        from dial_install.runner import Result
        instance, calls, capture, sleep = self.flaky(0)
        with capture, sleep, self.assertRaises(CommandError):
            from unittest import mock
            with mock.patch.object(Runner, "_capture", staticmethod(lambda a, i, e: Result(1, "", "AccessDenied"))):
                instance.run(["aws", "s3", "ls"])


class RunnerTests(Base):
    def test_capture(self):
        result = Runner().run([sys.executable, "-c", "print('hi')"])
        self.assertEqual(result.stdout.strip(), "hi")

    def test_stream_echoes_live_and_captures(self):
        shown = []
        runner = Runner(echo=shown.append)
        result = runner.run([sys.executable, "-c", "print('one'); print('two')"], stream=True)
        self.assertEqual([line.strip() for line in shown], ["one", "two"])
        self.assertIn("two", result.stdout)

    def test_stream_failure_carries_output(self):
        with self.assertRaises(CommandError) as raised:
            Runner(echo=lambda _l: None).run([sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"], stream=True)
        self.assertEqual(raised.exception.returncode, 3)
        self.assertIn("boom", raised.exception.stderr)

    def test_stream_passes_stdin(self):
        shown = []
        Runner(echo=shown.append).run([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
                                      stream=True, input_text="abc")
        self.assertEqual(shown[0].strip(), "ABC")

    def test_dry_run_does_not_execute_writes(self):
        shown = []
        runner = Runner(dry_run=True, echo=shown.append)
        runner.run([sys.executable, "-c", "raise SystemExit(9)"], write=True)       # would fail if executed
        self.assertIn("[dry-run]", shown[0])

    def test_dry_run_still_executes_reads(self):
        self.assertEqual(Runner(dry_run=True).run([sys.executable, "-c", "print(1)"]).stdout.strip(), "1")

    def test_missing_command_is_a_clear_error(self):
        with self.assertRaises(CommandError) as raised:
            Runner().run(["definitely-not-a-command-xyz"])
        self.assertIn("not found", raised.exception.stderr)

    def test_check_false_returns_failure(self):
        self.assertEqual(Runner().run([sys.executable, "-c", "raise SystemExit(2)"], check=False).returncode, 2)


class HygieneTests(Base):
    def python_files(self):
        for folder in ("src", "modules", "tests"):
            yield from (ROOT / folder).rglob("*.py")
        yield ROOT / "build.py"

    def test_python_39_syntax(self):
        for path in self.python_files():
            try:
                ast.parse(path.read_text(), filename=str(path), feature_version=(3, 9))
            except SyntaxError as error:
                self.fail(f"{path.relative_to(ROOT)} is not valid for Python 3.9: {error}")

    def test_no_unused_imports_in_package(self):
        problems = []
        for path in (ROOT / "src").rglob("*.py"):
            if path.name == "__init__.py":
                continue
            tree = ast.parse(path.read_text())
            imported = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imported[(alias.asname or alias.name).split(".")[0]] = node.lineno
                elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
                    for alias in node.names:
                        imported[alias.asname or alias.name] = node.lineno
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
                    {n.value.id for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
            source = path.read_text()
            for name, line in imported.items():
                if name not in names and source.count(name) < 2:
                    problems.append(f"{path.relative_to(ROOT)}:{line} unused import {name}")
        self.assertEqual(problems, [])

    def test_the_installer_code_hard_codes_no_deployment_region(self):
        region = re.compile(r"\b(?:us|eu|ap|il|me|sa|ca|af|mx)-(?:east|west|north|south|central|northeast|southeast)-\d\b")
        allowed = {("wizard.py", "us-east-1")}            # only the default suggested when no AWS_REGION is set
        for path in list((ROOT / "src").rglob("*.py")) + list((ROOT / "modules").rglob("*.py")):
            for match in region.findall(path.read_text()):
                self.assertIn((path.name, match), allowed, f"{path.name} hard-codes the region {match}")

    def test_customer_facing_text_has_no_hebrew(self):
        for path in list((ROOT / "src").rglob("*.py")) + list((ROOT / "docs").rglob("*.md")) + \
                list((ROOT / "modules").rglob("*.py")) + [ROOT / "README.md", ROOT / "installer.yaml"]:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                self.assertFalse(any("֐" <= ch <= "׿" for ch in line), f"{path.name}:{number}")


if __name__ == "__main__":
    unittest.main()
