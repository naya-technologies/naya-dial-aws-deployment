"""The documentation lists every command and option the CLI has."""

import re
import unittest
from pathlib import Path

from helpers import Base, ROOT
from dial_install import cli


def commands() -> dict:
    parser = cli._parser()
    action = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    return action.choices


class CommandReferenceTests(Base):
    def test_readme_lists_every_command(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for name in commands():
            self.assertRegex(readme, rf"`{re.escape(name)}[ `<]|\./dial-install {re.escape(name)}\b", f"README does not mention `{name}`")

    def test_readme_lists_every_option(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for name, sub in commands().items():
            for action in sub._actions:
                for flag in action.option_strings:
                    if flag.startswith("--") and flag != "--help":
                        self.assertIn(flag, readme, f"README does not document {flag} of `{name}`")

    def test_every_linked_document_exists(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for target in re.findall(r"\]\((docs/[A-Za-z0-9_.-]+\.md)", readme):
            self.assertTrue((ROOT / target).is_file(), target)


if __name__ == "__main__":
    unittest.main()
