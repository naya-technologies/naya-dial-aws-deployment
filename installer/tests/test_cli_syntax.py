"""Every aws / kubectl / helm command the installer runs must use real sub-commands and flags.

The local CLIs describe themselves offline (no credentials, no network), so a full scripted `apply`
is replayed against their own help text. This catches typos in flags that no mock can.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from functools import lru_cache
from pathlib import Path

import yaml

from helpers import Base, ROOT
from test_workflow import run_full_apply

ENV = {**os.environ, "AWS_PAGER": "", "MANPAGER": "cat", "PAGER": "cat", "TERM": "dumb", "MAN_KEEP_FORMATTING": "0"}
AWS_GLOBAL = {"--region", "--profile", "--output", "--query", "--debug", "--no-verify-ssl", "--endpoint-url"}


def clean(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m|.\x08", "", text)


@lru_cache(maxsize=None)
def help_text(*argv):
    result = subprocess.run(argv, capture_output=True, text=True, env=ENV, timeout=60)
    return result.returncode, clean(result.stdout + result.stderr)


def long_flags(argv):
    return [a.split("=")[0] for a in argv if a.startswith("--") and len(a) > 2]


@unittest.skipUnless(shutil.which("aws") and shutil.which("kubectl") and shutil.which("helm"), "needs aws, kubectl and helm installed")
class CommandSyntaxTests(Base):
    @classmethod
    def setUpClass(cls):
        # every optional feature switched on, so every command shape the installer can produce is replayed
        raw = yaml.safe_load((ROOT / "examples" / "dial.minimal.yaml").read_text())
        raw["network"] = {"allowed_cidrs": ["10.0.0.0/8", "172.16.0.0/12"]}
        raw["tags"] = {"CostCenter": "4711"}
        raw["kubernetes"] = {"admin_arns": ["arn:aws:iam::123456789012:role/PlatformTeam"], "public_endpoint": False}
        raw["database"] = {"auth": "iam", "backup_retention_days": 14, "deletion_protection": False}
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        yaml.safe_dump(raw, handle)
        handle.close()
        cls.config = Path(handle.name)
        cls.runner, cls.code, cls.say = run_full_apply(cls.config)

    @classmethod
    def tearDownClass(cls):
        cls.config.unlink()

    def test_the_scripted_apply_succeeded(self):
        self.assertEqual(self.code, 0, self.say.text)
        self.assertGreater(len(self.runner.calls), 60)

    def test_aws_commands(self):
        seen = set()
        for argv in self.runner.calls:
            if argv[0] != "aws":
                continue
            service, command = argv[1], argv[2]
            code, text = help_text("aws", service, command, "help")
            self.assertEqual(code, 0, f"aws {service} {command} is not a real command")
            for flag in long_flags(argv[3:]):
                if flag in AWS_GLOBAL:
                    continue
                self.assertIn(flag, text, f"aws {service} {command} has no option {flag}")
            seen.add((service, command))
        self.assertGreaterEqual(len(seen), 12)

    def kubectl_verb(self, argv):
        rest = argv[1:]
        position = 0
        while position < len(rest) and rest[position].startswith("-"):
            position += 2 if rest[position] in ("--context", "-n", "--namespace") else 1
        return rest[position], rest[position + 1] if position + 1 < len(rest) else ""

    def test_kubectl_commands(self):
        for argv in self.runner.calls:
            if argv[0] != "kubectl":
                continue
            verb, nxt = self.kubectl_verb(argv)
            args = (verb, nxt) if (verb, nxt) in {("rollout", "status"), ("rollout", "restart"), ("set", "resources")} else (verb,)
            code, text = help_text("kubectl", *args, "--help")
            self.assertEqual(code, 0, f"kubectl {' '.join(args)} is not a real command")
            # Everything after a bare `--` is the command run inside the container, not kubectl options.
            own = argv[1:argv.index("--")] if "--" in argv else argv[1:]
            for flag in long_flags(own):
                if flag in ("--context", "--namespace"):
                    continue
                self.assertIn(flag, text, f"kubectl {' '.join(args)} has no option {flag}")

    def test_helm_commands(self):
        for argv in self.runner.calls:
            if argv[0] != "helm":
                continue
            verb = argv[1:3] if argv[1] in ("registry", "show", "get") else argv[1:2]
            code, text = help_text("helm", *verb, "--help")
            self.assertEqual(code, 0, f"helm {' '.join(verb)} is not a real command")
            for flag in long_flags(argv[2:]):
                if flag == "--kube-context":
                    continue
                self.assertIn(flag, text, f"helm {' '.join(verb)} has no option {flag}")

    def test_replay_included_the_optional_features(self):
        calls = [" ".join(c) for c in self.runner.calls]
        self.assertTrue(any("eks create-access-entry" in c for c in calls))
        self.assertTrue(any("eks associate-access-policy" in c for c in calls))
        self.assertTrue(any("wait --for=condition=complete job/dial-admin-rds-iam-bootstrap" in c for c in calls))   # IAM login role bootstrap
        deploys = [c for c in self.runner.calls if c[:3] == ["aws", "cloudformation", "deploy"]]
        self.assertTrue(all("CostCenter=4711" in c for c in deploys))
        self.assertTrue(any("AllowedCidr2=172.16.0.0/12" in c for c in deploys))

    def test_no_istioctl_and_no_download_in_the_replay(self):
        self.assertFalse([c for c in self.runner.calls if c and c[0].endswith("istioctl")])
        server_side = [c for c in self.runner.writes if "--server-side" in c]
        self.assertGreaterEqual(len(server_side), 2)               # Knative CRDs and the Istio manifest are too large for client-side apply


if __name__ == "__main__":
    unittest.main()
