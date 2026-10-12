"""Run cfn-lint on every template when it is installed (pip install cfn-lint). Errors fail; warnings do not."""

import shutil
import subprocess
import unittest

from helpers import Base, ROOT

# Pre-existing, reviewed warnings inherited from the original templates. Anything new fails the build.
ACCEPTED_WARNINGS = {"W2001", "W3005"}


@unittest.skipUnless(shutil.which("cfn-lint"), "cfn-lint is not installed")
class LintTests(Base):
    def test_templates_have_no_lint_errors_or_new_warnings(self):
        problems = []
        for template in sorted((ROOT / "modules").glob("*/*/stack.yaml")):
            result = subprocess.run(["cfn-lint", str(template), "--regions", "us-east-1"], capture_output=True, text=True)
            for line in result.stdout.splitlines():
                code = line.split(" ", 1)[0]
                if code[:1] in "EW" and code[1:].isdigit() and code not in ACCEPTED_WARNINGS:
                    problems.append(f"{template.relative_to(ROOT)}: {line}")
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
