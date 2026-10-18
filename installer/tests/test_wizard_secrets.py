import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from helpers import Base, RELEASE
from dial_install import config, secrets, wizard
from dial_install.errors import InstallerError


def scripted(answers):
    queue = list(answers)
    return lambda _prompt: queue.pop(0)


CERT = "arn:aws:acm:us-east-1:123456789012:certificate/00000000-0000-0000-0000-000000000000"
SECRET = "arn:aws:secretsmanager:us-east-1:123456789012:secret:dial/x-AbCdEf"


class SecretTests(Base):
    def test_generated_document_is_complete_and_valid(self):
        data = secrets.generate()
        self.assertEqual(set(data), {k.name for k in secrets.REQUIRED_KEYS})
        self.assertTrue(all(v == "present" for v in secrets.audit(data).values()))
        self.assertGreaterEqual(len(data["dashboards_signing_secret"]), 32)
        self.assertGreaterEqual(len(data["grafana_admin_password"]), 12)
        self.assertEqual(len({v for v in data.values()}), len(data), "values must not repeat")

    def test_db_password_is_url_and_rds_safe(self):
        for _ in range(20):
            self.assertRegex(secrets.generate()["db_master_password"], r"^[A-Za-z0-9]{24}$")

    def test_audit_reports_missing_placeholder_short(self):
        data = secrets.generate()
        del data["dial_api_key"]
        data["influxdb_token"] = "CHANGEME"
        data["dashboards_signing_secret"] = "short"
        status = secrets.audit(data)
        self.assertEqual(status["dial_api_key"], "missing")
        self.assertEqual(status["influxdb_token"], "placeholder")
        self.assertIn("too short", status["dashboards_signing_secret"])

    def test_create_command(self):
        self.assertEqual(
            secrets.create_command("acme", "us-east-1"),
            "aws secretsmanager create-secret --name dial/acme --secret-string file://dial-secrets.json --region us-east-1")


class WizardTests(Base):
    def run_wizard(self, answers):
        return wizard.Wizard(ask=scripted(answers), say=lambda _m: None).run()

    def test_simple_flow(self):
        answers = self.run_wizard(["acme", "us-east-1", "", "dial.acme.example", "y", CERT,
                                   "1", "", "1", "", "1", "1", "y", "y", SECRET])
        text = wizard.render_config(answers)
        path = self.write(text)
        cfg = config.resolve(config.load(path), RELEASE)          # passes full schema validation
        self.assertEqual(cfg["domain"]["hosts"]["chat"], "chat.dial.acme.example")
        self.assertEqual(cfg["network"]["provider"], "create")
        self.assertEqual(config.semantic_errors(cfg), [])

    def test_existing_vpc_saml_custom_hosts(self):
        hosts = ["h-chat.example.org", "h-admin.example.org", "h-core.example.org", "h-themes.example.org",
                 "h-grafana.example.org", "h-superset.example.org", "h-flows.example.org", "h-flows-core.example.org"]
        answers = self.run_wizard([
            "corp", "us-east-1", "", "example.org", "n", *hosts, CERT,
            "2", "vpc-0123456789abcdef0", "subnet-0a1,subnet-0b2", "subnet-0c3,subnet-0d4",
            "2", "10.0.0.0/8, 172.16.0.0/12",
            "2", "CorpIdP", "https://idp.example.org/metadata.xml", "y",
            "2", "n", "n", SECRET])
        cfg = config.resolve(config.load(self.write(wizard.render_config(answers))), RELEASE)
        self.assertEqual(cfg["domain"]["hosts"]["chat"], "h-chat.example.org")
        self.assertEqual(cfg["network"]["subnets"]["private"], ["subnet-0a1", "subnet-0b2"])
        self.assertEqual(cfg["network"]["exposure"], "public")
        self.assertEqual(cfg["network"]["allowed_cidrs"], ["10.0.0.0/8", "172.16.0.0/12"])
        self.assertEqual(cfg["identity"]["provider"], "cognito-saml")
        self.assertFalse(cfg["features"]["analytics"])
        self.assertEqual(cfg["database"]["auth"], "iam")
        self.assertEqual(config.semantic_errors(cfg), [])

    def test_blank_secret_arn_leaves_a_todo_that_check_refuses(self):
        answers = self.run_wizard(["acme", "us-east-1", "", "dial.acme.example", "y", CERT, "1", "", "1", "", "1", "1", "y", "y", ""])
        path = self.write(wizard.render_config(answers))
        with self.assertRaises(InstallerError) as raised:
            config.load(path)
        self.assertIn("secrets.arn", raised.exception.message)

    def test_reasks_on_bad_input(self):
        answers = self.run_wizard(["BAD NAME", "acme", "us-east-1", "", "nodomain", "dial.acme.example", "y", "bad-arn", CERT,
                                   "1", "", "1", "", "9", "1", "1", "y", "y", SECRET])
        self.assertEqual(answers["name"], "acme")
        self.assertEqual(answers["domain"]["base"], "dial.acme.example")

    def test_secrets_file_is_private_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            message = wizard.write_secrets_file(Path(directory), "acme", "us-east-1")
            path = Path(directory) / wizard.SECRETS_FILE
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertTrue(all(v == "present" for v in secrets.audit(json.loads(path.read_text())).values()))
            self.assertIn("aws secretsmanager create-secret", message)
            with self.assertRaises(InstallerError):
                wizard.write_secrets_file(Path(directory), "acme", "us-east-1")

    def test_init_never_overwrites_config(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "dial.yaml").write_text("keep me")
            answers = self.run_wizard(["acme", "us-east-1", "", "dial.acme.example", "y", CERT, "1", "", "1", "", "1", "1", "y", "y", SECRET])
            with self.assertRaises(InstallerError):
                wizard.write_outputs(answers, Path(directory), lambda _m: None)
            self.assertEqual((Path(directory) / "dial.yaml").read_text(), "keep me")

    def write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return Path(handle.name)


if __name__ == "__main__":
    unittest.main()
