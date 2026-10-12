"""Database sign-in with the IAM role (database.auth: iam) as an option next to the password."""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from helpers import Base, CHART_VALUES_TEXT, FAKE_OUTPUTS, PLATFORM_CHART, ROOT, make_ctx, resolved
from dial_install import config, db_bootstrap, deploy, mirror, modules, values
from dial_install.errors import InstallerError
from dial_install.runner import FakeRunner, Result
from dial_install.schema import validate

CHART_VALUES = yaml.safe_load(CHART_VALUES_TEXT)
CHART_CHARTS_TEXT = CHART_VALUES_TEXT + (PLATFORM_CHART / "Chart.yaml").read_text()
HAVE_HELM = shutil.which("helm") is not None


def iam_ctx(**database):
    return make_ctx(cfg=resolved(database={"auth": "iam", **database}))


class ConfigTests(Base):
    def test_password_is_the_default(self):
        cfg = resolved()
        self.assertEqual((cfg["database"]["auth"], cfg["database"]["iam_user"]), ("password", "dial_admin_iam"))

    def test_schema_accepts_iam_and_rejects_anything_else(self):
        schema = config.load_schema()
        raw = {"apiVersion": "dial/v1", "name": "acme", "aws": {"region": "us-east-1"},
               "domain": {"base": "dial.acme.example", "certificate_arn": "arn:aws:acm:us-east-1:123456789012:certificate/x"},
               "secrets": {"arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:s-AbCdEf"}}
        self.assertEqual(validate({**raw, "database": {"auth": "iam", "iam_user": "dial_admin_iam"}}, schema), [])
        self.assertTrue(validate({**raw, "database": {"auth": "kerberos"}}, schema))
        self.assertTrue(validate({**raw, "database": {"iam_user": "Bad-Name"}}, schema))

    def test_the_iam_stack_authorises_the_same_database_user(self):
        module = next(m for m in modules.discover() if m.slot == "iam")
        params = module.parameters(iam_ctx(iam_user="dial_admin"))
        self.assertEqual(params["AdminDatabaseUser"], "dial_admin")
        self.assertEqual(module.parameters(make_ctx())["AdminDatabaseUser"], "dial_admin_iam")


class ValuesTests(Base):
    def admin(self, ctx):
        return values.build(ctx, CHART_VALUES)["admin"]

    def test_password_mode_is_unchanged(self):
        built = values.build(make_ctx(), CHART_VALUES)
        self.assertEqual(built["admin"]["externalDatabase"]["user"], "postgres")
        self.assertNotEqual(built["admin"]["externalDatabase"]["password"], "")
        self.assertNotIn("datasourceAuth", json.dumps(built["admin"]["backend"].get("configuration", {})))
        self.assertNotIn("adminRdsIam", built)

    def test_iam_mode_replaces_the_password_with_the_role(self):
        ctx = iam_ctx()
        built = values.build(ctx, CHART_VALUES)
        admin = built["admin"]
        self.assertEqual(admin["externalDatabase"], {"host": "db.cluster.rds.amazonaws.com", "database": "dialadmin",
                                                      "user": "dial_admin_iam", "password": ""})       # port 5432 stays the chart default
        self.assertEqual(admin["backend"]["configuration"]["datasourceAuth"], {"type": "aws", "aws": {"region": "us-east-1"}})
        self.assertEqual(admin["backend"]["serviceAccount"]["name"], "dial-admin-backend")
        self.assertEqual(admin["backend"]["serviceAccount"]["annotations"]["eks.amazonaws.com/role-arn"], FAKE_OUTPUTS["iam"]["AdminBackendRDSRoleArn"])
        self.assertEqual(admin["backend"]["env"]["USE_SYSTEM_CA_CERTS"], "true")
        self.assertTrue(built["adminRdsIam"]["caBundle"]["enabled"])
        self.assertNotIn(ctx.secret["db_master_password"], json.dumps(admin))

    def test_iam_user_is_configurable(self):
        self.assertEqual(self.admin(iam_ctx(iam_user="dial_admin"))["externalDatabase"]["user"], "dial_admin")

    def test_other_components_keep_their_password(self):
        ctx = iam_ctx()
        built = values.build(ctx, CHART_VALUES)
        self.assertEqual(built["deployment-manager"]["secrets"]["POSTGRES_DATASOURCE_PASSWORD"], ctx.secret["db_master_password"])
        self.assertIn(ctx.secret["db_master_password"].replace("/", "%2F"), built["superset"]["secrets"]["SUPERSET_METADATA_DATABASE_URI"])

    def test_the_chart_itself_names_only_repositories_of_the_listing(self):
        """The Marketplace may scan the chart for images: the optional bootstrap image lives in the release manifest instead."""
        self.assertNotIn("naya-dial-postgres", CHART_CHARTS_TEXT)

    def test_the_profile_never_reaches_helm(self):
        self.assertNotIn("installer", values.build(iam_ctx(), CHART_VALUES))

    def test_no_placeholder_is_left(self):
        self.assertNotIn("PLACEHOLDER", json.dumps(values.build(iam_ctx(), CHART_VALUES)))


@unittest.skipUnless(HAVE_HELM, "helm is not installed")
class RenderedChartTests(Base):
    @staticmethod
    def holds(doc, secret):
        """True when `secret` appears in the resource, in clear text or inside a Kubernetes Secret's base64 data."""
        import base64
        text = json.dumps(doc)
        if secret in text:
            return True
        for value in (doc.get("data") or {}).values():
            try:
                if secret in base64.b64decode(value).decode("utf-8", "ignore"):
                    return True
            except Exception:  # noqa: BLE001 - not base64 data
                pass
        return False

    def render(self, ctx):
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        yaml.safe_dump(values.build(ctx, CHART_VALUES), handle)
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        result = subprocess.run(["helm", "template", "dial", str(PLATFORM_CHART), "--namespace", "dial", "-f", handle.name],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [d for d in yaml.safe_load_all(result.stdout) if d]

    def test_admin_backend_runs_with_its_role_and_the_ca_bundle(self):
        docs = self.render(iam_ctx())
        accounts = {d["metadata"]["name"]: d for d in docs if d["kind"] == "ServiceAccount"}
        self.assertTrue(accounts["dial-admin-backend"]["metadata"]["annotations"]["eks.amazonaws.com/role-arn"].endswith("AdminBackendRDSRoleArn"))
        backend = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "dial-admin-backend")
        pod = backend["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "dial-admin-backend")
        mounts = [m["mountPath"] for c in pod["containers"] for m in c.get("volumeMounts", [])]
        self.assertIn("/certificates/aws-rds-bundle.crt", mounts)
        bundle = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "dial-admin-rds-ca")
        self.assertGreaterEqual(bundle["data"]["aws-rds-bundle.crt"].count("BEGIN CERTIFICATE"), 100)

    def test_no_database_password_in_any_admin_resource(self):
        ctx = iam_ctx()
        docs = self.render(ctx)
        password = ctx.secret["db_master_password"]
        for d in docs:
            if d["metadata"]["name"].startswith("dial-admin"):
                self.assertFalse(self.holds(d, password), f"{d['kind']}/{d['metadata']['name']} contains the database password")
        # the deployment manager and Superset still use the password (they have no IAM login); that is expected
        holders = {d["metadata"]["name"] for d in docs if self.holds(d, password)}
        self.assertEqual(holders, {"dial-deployment-manager", "dial-superset"})

    def test_password_mode_creates_no_ca_bundle_and_keeps_the_password(self):
        ctx = make_ctx()
        docs = self.render(ctx)
        self.assertFalse([d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "dial-admin-rds-ca"])
        holders = {d["metadata"]["name"] for d in docs if self.holds(d, ctx.secret["db_master_password"])}
        # also the positive control for the IAM-mode check above: the same search does find the password here
        self.assertEqual(holders, {"dial-admin-externaldb", "dial-deployment-manager", "dial-superset"})

    def test_the_role_trusts_exactly_the_service_account_the_chart_creates(self):
        from test_modules import template
        iam = template(next(m for m in modules.discover() if m.slot == "iam"))
        trust = iam["Resources"]["AdminBackendRDSRole"]["Properties"]["AssumeRolePolicyDocument"]["Sub"][0]
        self.assertIn("system:serviceaccount:${ServiceAccountNamespace}:${ServiceAccountName}", trust)
        default = iam["Parameters"]["AdminBackendServiceAccountName"]["Default"]
        docs = self.render(iam_ctx())
        self.assertIn(default, {d["metadata"]["name"] for d in docs if d["kind"] == "ServiceAccount"})


class BootstrapJobTests(Base):
    def run_bootstrap(self, ctx=None, responses=None):
        ctx = ctx or iam_ctx()
        ctx.cloud.runner = FakeRunner({("helm", "show", "values"): Result(0, CHART_VALUES_TEXT), **(responses or {})})
        effective = values.effective(ctx, CHART_VALUES)
        say = []
        db_bootstrap.run(ctx, effective, say.append)
        return ctx, say

    def test_manifests(self):
        ctx = iam_ctx()
        docs = db_bootstrap.manifests(ctx, "registry/postgres:15")
        job = docs["job"]["spec"]["template"]["spec"]
        container = job["containers"][0]
        env = {e["name"]: e for e in container["env"]}
        self.assertEqual(env["PGUSER"]["value"], "postgres")
        self.assertEqual(env["PGSSLMODE"]["value"], "verify-full")
        self.assertEqual(env["ADMIN_DATABASE_USER"]["value"], "dial_admin_iam")
        self.assertIn("secretKeyRef", env["PGPASSWORD"]["valueFrom"])
        self.assertNotIn("value", env["PGPASSWORD"])                      # the password is never inline in the Job
        self.assertEqual(docs["secret"]["stringData"]["password"], ctx.secret["db_master_password"])
        self.assertTrue(job["securityContext"]["runAsNonRoot"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        for statement in ("CREATE ROLE", "PASSWORD NULL", "GRANT rds_iam", "ALTER SCHEMA", "GRANT USAGE, CREATE ON SCHEMA"):
            self.assertIn(statement, container["args"][0])

    def test_the_bundled_ca_matches_the_chart_bundle(self):
        self.assertEqual((ROOT / "assets" / "aws-rds-global-bundle.pem").read_bytes(),
                         (PLATFORM_CHART / "files" / "aws-rds-global-bundle.pem").read_bytes())

    def test_sequence_and_cleanup(self):
        ctx, say = self.run_bootstrap()
        calls = [" ".join(c[3:8]) for c in ctx.cloud.runner.calls if c[0] == "kubectl"]
        self.assertTrue(calls[0].startswith("-n dial delete job"))                      # leftovers removed first
        applies = [c for c in ctx.cloud.runner.writes if c[-2:] == ["-f", "-"]]
        self.assertEqual(len(applies), 3)
        self.assertTrue(any("wait" in c and "--for=condition=complete" in c for c in ctx.cloud.runner.writes))
        self.assertTrue(" ".join(ctx.cloud.runner.calls[-1]).find("delete") > 0, "temporary resources are removed at the end")
        self.assertTrue(any("database role is ready" in line or "iam_role=" in line for line in say))

    def test_the_password_never_appears_on_a_command_line(self):
        ctx, _ = self.run_bootstrap()
        password = ctx.secret["db_master_password"]
        self.assertFalse([c for c in ctx.cloud.runner.calls if any(password in part for part in c)])

    def test_failure_reports_the_job_logs_and_still_cleans_up(self):
        ctx = iam_ctx()
        ctx.cloud.runner = FakeRunner({
            ("helm", "show", "values"): Result(0, CHART_VALUES_TEXT),
            ("kubectl", "--context", "dial-acme", "-n", "dial", "wait"): Result(1, "", "timed out"),
            ("kubectl", "--context", "dial-acme", "-n", "dial", "logs"): Result(0, "psql: error: connection refused"),
        })
        with self.assertRaises(InstallerError) as raised:
            db_bootstrap.run(ctx, values.effective(ctx, CHART_VALUES), lambda _m: None)
        self.assertIn("connection refused", raised.exception.message)
        self.assertIn("describe job", raised.exception.hint)
        self.assertIn("delete", " ".join(ctx.cloud.runner.calls[-1]))

    def test_image_defaults_to_the_release_pin_and_can_be_overridden(self):
        ctx = iam_ctx()
        effective = values.effective(ctx, CHART_VALUES)
        self.assertEqual(db_bootstrap.image(ctx, effective),
                         "123456789012.dkr.ecr.us-east-1.amazonaws.com/naya-technologies-by-epam/naya-dial-postgres:15-alpine")
        custom = iam_ctx(bootstrap_image="registry.example/postgres:16")
        self.assertEqual(db_bootstrap.image(custom, effective), "registry.example/postgres:16")

    def test_an_unsafe_database_user_is_refused(self):
        with self.assertRaises(InstallerError):
            db_bootstrap.manifests(iam_ctx(iam_user="x; DROP ROLE postgres"), "img")


class HookTests(Base):
    def module(self, ctx):
        return next(m for m in modules.select(ctx.cfg) if m.slot == "database")

    def test_nothing_runs_in_password_mode(self):
        ctx = make_ctx()
        ctx.cloud.runner = FakeRunner({("helm", "show", "values"): Result(0, CHART_VALUES_TEXT)})
        self.module(ctx).before_helm(ctx, lambda _m: None)
        self.assertEqual(ctx.cloud.runner.writes, [])

    def test_the_role_is_created_in_iam_mode(self):
        ctx = iam_ctx()
        ctx.cloud.runner = FakeRunner({("helm", "show", "values"): Result(0, CHART_VALUES_TEXT)})
        self.module(ctx).before_helm(ctx, lambda _m: None)
        self.assertTrue(any("wait" in w for w in ctx.cloud.runner.writes))

    def test_check_mentions_the_iam_login(self):
        ctx = iam_ctx()
        ctx.cloud.runner = FakeRunner({("aws", "rds", "describe-db-engine-versions"):
                                       Result(0, json.dumps({"DBEngineVersions": [{"Status": "available"}]}))})
        text = " ".join(f.message for f in self.module(ctx).check(ctx))
        self.assertIn("IAM role", text)


class FlowTests(Base):
    def apply(self, database):
        from test_workflow import run_full_apply
        raw = yaml.safe_load((ROOT / "examples" / "dial.minimal.yaml").read_text())
        raw["database"] = database
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        yaml.safe_dump(raw, handle)
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        runner, code, say = run_full_apply(Path(handle.name))
        self.assertEqual(code, 0, say.text)
        return runner

    def test_iam_mode_creates_the_role_after_the_cluster_is_prepared_and_before_helm(self):
        runner = self.apply({"auth": "iam"})
        writes = [" ".join(w) for w in runner.writes]
        index = lambda needle: next(i for i, w in enumerate(writes) if needle in w)          # noqa: E731
        namespace_ready = index("dial-cluster-base")
        autoscaler = index("cluster-autoscaler")
        job = index("wait --for=condition=complete job/dial-admin-rds-iam-bootstrap")
        platform = index("upgrade --install dial ")
        self.assertLess(namespace_ready, job)
        self.assertLess(autoscaler, job)
        self.assertLess(job, platform)

    def test_password_mode_runs_no_database_job(self):
        runner = self.apply({})
        self.assertFalse([w for w in runner.writes if "dial-admin-rds-iam-bootstrap" in " ".join(w)])

    def test_the_password_is_sent_through_stdin_only(self):
        runner = self.apply({"auth": "iam"})
        secrets_doc = [c for c in runner.calls if c[:2] == ["kubectl", "--context"] and c[-2:] == ["-f", "-"]]
        self.assertTrue(secrets_doc)


class MirrorTests(Base):
    @unittest.skipUnless(HAVE_HELM, "helm is not installed")
    def test_the_bootstrap_image_is_copied_only_when_it_is_used(self):
        from test_mirror import runner_for, TOTAL
        for cfg, expected in ((resolved(), TOTAL), (resolved(database={"auth": "iam"}), TOTAL + 1),
                              (resolved(database={"auth": "iam", "bootstrap_image": "x/y:1"}), TOTAL)):
            ctx = make_ctx(cfg=cfg)
            ctx.cloud.runner = runner_for()
            images = mirror.discover(ctx, CHART_VALUES)
            self.assertEqual(len(images), expected)
        self.assertTrue(any("naya-dial-postgres" in i for i in mirror.discover(_ctx_iam(), CHART_VALUES)))


def _ctx_iam():
    from test_mirror import runner_for
    ctx = iam_ctx()
    ctx.cloud.runner = runner_for()
    return ctx


if __name__ == "__main__":
    unittest.main()
