"""End-to-end command tests with a scripted fake AWS / Kubernetes / Helm."""

import json
import unittest
from pathlib import Path
from unittest import mock

import yaml

from helpers import Base, CHART_VALUES_TEXT, FAKE_OUTPUTS, PLATFORM_CHART, RELEASE, ROOT, SECRET_ARN, make_ctx, ok_json, resolved
from dial_install import checks, deploy, secrets, tools, workflow
from dial_install.runner import FakeRunner, Result

CERT_NAMES = ["*.dial.acme.example"]
INFRA_REVISION = {"value": RELEASE["infra_revision"]}      # what the fake stacks report; tests change it to simulate older stacks


def stack_outputs(argv):
    stack = argv[argv.index("--stack-name") + 1]
    slot = stack.removeprefix("acme-")
    raw = FAKE_OUTPUTS.get(slot)
    tags = [{"Key": "dial-install:infra-revision", "Value": str(INFRA_REVISION["value"])}]
    outputs = [{"OutputKey": k, "OutputValue": v} for k, v in (raw or {}).items()]
    return Result(0, json.dumps({"Stacks": [{"StackStatus": "CREATE_COMPLETE", "Outputs": outputs, "Tags": tags}]}))


def aws_fakes(secret_doc=None, deployed=True):
    answers = {
        ("aws", "sts", "get-caller-identity"): ok_json({"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/me"}),
        ("aws", "secretsmanager", "get-secret-value"): Result(0, json.dumps(secret_doc or secrets.generate())),
        ("aws", "acm", "describe-certificate"): ok_json({"Certificate": {"Status": "ISSUED", "SubjectAlternativeNames": CERT_NAMES}}),
        ("aws", "ec2", "describe-instance-type-offerings"): ok_json({"InstanceTypeOfferings": [
            {"InstanceType": t} for t in ("c6i.xlarge", "c6i.large")]}),
        ("aws", "ecr", "get-login-password"): Result(0, "token"),
        ("aws", "cognito-idp", "describe-user-pool-client"): ok_json({"UserPoolClient": {"ClientSecret": "cognito-secret"}}),
        ("aws", "route53", "create-hosted-zone"): ok_json({"HostedZone": {"Id": "/hostedzone/Z1"}}),
    }
    if deployed:
        answers[("aws", "cloudformation", "describe-stacks")] = stack_outputs
    return answers


def real_helm_template(argv):
    """Render the local chart with the real helm (what the Marketplace chart would render to)."""
    import subprocess
    position = argv.index("template")
    rest = argv[position + 3:]
    if "--version" in rest:
        index = rest.index("--version")
        del rest[index:index + 2]
    proc = subprocess.run(["helm", "template", argv[position + 1], str(PLATFORM_CHART), *rest], capture_output=True, text=True)
    return Result(proc.returncode, proc.stdout, proc.stderr)


KNATIVE_SAMPLE = (ROOT / "tests" / "fixtures" / "knative-sample.yaml").read_text()
SOURCE_HOST = "709825985650.dkr.ecr.us-east-1.amazonaws.com"
MIRRORED = set()          # destination refs "copied" so far (reset by run_full_apply)


def crane_digest(argv):
    ref = argv[2]
    if ref.startswith(SOURCE_HOST):
        return Result(0, "sha256:" + "a" * 64 + "\n")
    return Result(0, "sha256:" + "a" * 64 + "\n") if ref in MIRRORED else Result(1, "", "MANIFEST_UNKNOWN")


def crane_copy(argv):
    MIRRORED.add(argv[3])
    return Result(0)


def registry_fakes():
    return {
        ("helm", "template"): real_helm_template,
        ("/usr/bin/crane", "digest"): crane_digest,
        ("/usr/bin/crane", "copy"): crane_copy,
        # listing works (permission probe); a lookup by name finds nothing, so every repository is created
        ("aws", "ecr", "describe-repositories"): lambda argv: Result(255, "", "RepositoryNotFoundException") if "--repository-names" in argv else Result(0, "{}"),
    }


def cluster_fakes(chart="naya-helm-deployment-2026.10.1"):
    return {
        ("kubectl", "--context", "dial-acme", "-n", "istio-system", "get", "svc"): ok_json(
            {"status": {"loadBalancer": {"ingress": [{"hostname": "istio.elb.amazonaws.com"}]}}}),
        ("kubectl", "--context", "dial-acme", "-n", "dial", "get", "deployment", "-o"): ok_json(
            {"items": [{"metadata": {"name": "dial-core"}}, {"metadata": {"name": "dial-lic-ins"}}]}),
        ("kubectl", "--context", "dial-acme", "-n", "dial", "get", "statefulset"): ok_json({"items": []}),
        ("kubectl", "--context", "dial-acme", "-n", "dial", "get", "pods"): ok_json(
            {"items": [{"metadata": {"name": "dial-lic-ins-7d9f", "creationTimestamp": "2026-01-01T00:00:00Z"}},
                    {"metadata": {"name": "dial-core-1", "creationTimestamp": "2026-01-02T00:00:00Z"}}]}),
        ("kubectl", "--context", "dial-acme", "-n", "dial", "logs"): Result(0, "starting\nSetting up...\nOK\n"),
        ("kubectl", "--context", "dial-acme", "-n", "dial", "get", "ingress"): ok_json({"items": [{
            "spec": {"rules": [{"host": "chat.dial.acme.example"}, {"host": "admin.dial.acme.example"}]},
            "status": {"loadBalancer": {"ingress": [{"hostname": "alb-1.elb.amazonaws.com"}]}}}]}),
        ("helm", "show", "values"): Result(0, CHART_VALUES_TEXT),
        ("helm", "list"): Result(0, json.dumps([{"chart": chart, "revision": "3", "status": "deployed"}])),
        ("helm", "get", "values"): Result(0, json.dumps({"global": {"region": "us-east-1"}, "x": {"secrets": {"a": "old"}}})),
    }


def options(**kw):
    return workflow.Options(config_path=ROOT / "examples" / "dial.minimal.yaml", **kw)


class Capture:
    def __init__(self):
        self.lines = []

    def __call__(self, message):
        self.lines.append(message)

    @property
    def text(self):
        return "\n".join(self.lines)


def patched_tools():
    return mock.patch.multiple(
        "dial_install.checks", shutil=mock.Mock(which=lambda tool: f"/usr/bin/{tool}"))


class CheckTests(Base):
    def run_check(self, extra=None, **aws_kw):
        responses = {**aws_fakes(deployed=False), **(extra or {})}
        runner = FakeRunner(responses)
        say = Capture()
        with patched_tools(), mock.patch.object(secrets, "fetch", wraps=secrets.fetch):
            code = workflow.cmd_check(options(), runner, say)
        return code, say.text, runner

    def test_healthy_environment_passes(self):
        code, text, _ = self.run_check()
        self.assertEqual(code, 0, text)
        self.assertIn("all 14 required keys are present", text)
        self.assertIn("issued and covers all 6 host names", text)

    def test_missing_secret_key_fails_with_fix(self):
        doc = secrets.generate()
        del doc["core_encryption_key"]
        code, text, _ = self.run_check({("aws", "secretsmanager", "get-secret-value"): Result(0, json.dumps(doc))})
        self.assertEqual(code, 1)
        self.assertIn("key core_encryption_key: missing", text)

    def test_certificate_not_covering_hosts(self):
        answers = {("aws", "acm", "describe-certificate"): ok_json(
            {"Certificate": {"Status": "ISSUED", "SubjectAlternativeNames": ["*.other.example"]}})}
        code, text, _ = self.run_check(answers)
        self.assertEqual(code, 1)
        self.assertIn("does not cover", text)

    def test_certificate_must_be_issued(self):
        answers = {("aws", "acm", "describe-certificate"): ok_json(
            {"Certificate": {"Status": "PENDING_VALIDATION", "SubjectAlternativeNames": CERT_NAMES}})}
        code, text, _ = self.run_check(answers)
        self.assertEqual(code, 1)
        self.assertIn("PENDING_VALIDATION", text)

    def test_wrong_account(self):
        runner = FakeRunner({**aws_fakes(deployed=False)})
        say = Capture()
        opts = options()
        with patched_tools(), mock.patch.object(workflow.config, "load", return_value={
                **yaml.safe_load((ROOT / "examples" / "dial.minimal.yaml").read_text()),
                "aws": {"region": "us-east-1", "account_id": "999999999999"}}):
            code = workflow.cmd_check(opts, runner, say)
        self.assertEqual(code, 1)
        self.assertIn("999999999999", say.text)

    def test_no_credentials_stops_early(self):
        code, text, _ = self.run_check({("aws", "sts", "get-caller-identity"): Result(255, "", "Unable to locate credentials")})
        self.assertEqual(code, 1)
        self.assertIn("cannot call AWS", text)
        self.assertNotIn("secret:", text)

    def test_missing_tool_is_reported_with_fix(self):
        runner = FakeRunner(aws_fakes(deployed=False))
        say = Capture()
        with mock.patch("dial_install.checks.shutil.which", return_value=None), mock.patch.object(tools, "which", return_value=None):
            code = workflow.cmd_check(options(), runner, say)
        self.assertEqual(code, 1)
        self.assertIn("install-tools", say.text)

    def test_check_changes_nothing(self):
        _, _, runner = self.run_check()
        self.assertEqual(runner.writes, [])

    def test_wildcard_matching_rules(self):
        self.assertTrue(checks._covers("*.example.com", "chat.example.com"))
        self.assertFalse(checks._covers("*.example.com", "a.b.example.com"))
        self.assertFalse(checks._covers("*.example.com", "example.com"))
        self.assertTrue(checks._covers("chat.example.com", "CHAT.example.com"))


class PlanTests(Base):
    def test_plan_lists_every_stack_and_changes_nothing(self):
        runner = FakeRunner(aws_fakes(deployed=False))
        say = Capture()
        with patched_tools():
            code = workflow.cmd_plan(options(), runner, say)
        self.assertEqual(code, 0, say.text)
        for slot in ("network", "endpoints", "storage", "compute", "links", "cache", "database", "iam", "identity"):
            self.assertIn(f"acme-{slot}", say.text)
        self.assertIn("CREATE", say.text)
        self.assertIn("https://chat.dial.acme.example", say.text)
        self.assertEqual(runner.writes, [])


def run_full_apply(config_path=None):
    """A non-dry-run `apply` against the scripted fake: stacks appear once `cloudformation deploy` ran."""
    MIRRORED.clear()
    runner = FakeRunner({**aws_fakes(), **cluster_fakes(), **registry_fakes()})
    state = {"deployed": False}

    def describe(argv):
        return stack_outputs(argv) if state["deployed"] else Result(255, "", "does not exist")

    original = runner.run

    def run(argv, **kw):
        if argv[:3] == ["aws", "cloudformation", "deploy"]:
            state["deployed"] = True
        return original(argv, **kw)

    runner.responses[("aws", "cloudformation", "describe-stacks")] = describe
    runner.run = run
    say = Capture()
    with patched_tools(), mock.patch.object(tools, "which", side_effect=lambda tool: f"/usr/bin/{tool}"), \
            mock.patch.object(tools, "download_text", return_value=KNATIVE_SAMPLE), mock.patch("time.sleep"):
        opts = options() if config_path is None else workflow.Options(config_path=config_path)
        code = workflow.cmd_apply(opts, runner, say, ask=lambda _p: "y")
    return runner, code, say


class ApplyTests(Base):
    def full_runner(self, dry_run=False):
        MIRRORED.clear()
        return FakeRunner({**aws_fakes(deployed=dry_run is False), **cluster_fakes(), **registry_fakes()}, dry_run=dry_run)

    def run_apply(self, runner, **kw):
        say = Capture()
        with patched_tools(), mock.patch.object(tools, "which", side_effect=lambda tool: f"/usr/bin/{tool}"), \
                mock.patch.object(tools, "download_text", return_value=KNATIVE_SAMPLE), mock.patch("time.sleep"):
            code = workflow.cmd_apply(options(**kw), runner, say, ask=lambda _p: "y")
        return code, say

    def test_full_apply_order_and_result(self):
        runner, code, say = run_full_apply()
        self.assertEqual(code, 0, say.text)

        kinds = []
        for argv in runner.writes:
            if argv[:3] == ["aws", "cloudformation", "deploy"]:
                kinds.append("stack:" + argv[argv.index("--stack-name") + 1])
            elif argv[0] == "helm" and argv[1] == "upgrade" and "dial" in argv:
                kinds.append("helm:dial")
            elif argv[0] == "helm" and "cluster-autoscaler" in argv:
                kinds.append("helm:autoscaler")
        self.assertEqual(kinds[:9], [f"stack:acme-{s}" for s in ("network", "endpoints", "storage", "compute", "links", "cache", "database", "iam", "identity")])
        self.assertLess(kinds.index("helm:autoscaler"), kinds.index("helm:dial"))
        self.assertIn("DIAL is installed.", say.text)
        self.assertIn("chat.dial.acme.example", say.text)
        self.assertIn("alb-1.elb.amazonaws.com", say.text)
        self.assertTrue(any(argv[:3] == ["kubectl", "--context", "dial-acme"] and "delete" in argv and "dial-lic-ins" in argv
                            for argv in runner.writes), "license installer must be removed after OK")

    def test_helm_is_never_run_with_wait_and_always_has_context(self):
        runner = self.full_runner()
        self.run_apply(runner)
        for argv in runner.calls:
            if argv[0] == "helm" and argv[1] not in ("registry", "show", "version", "template"):
                self.assertNotIn("--wait", argv)
                self.assertIn("--kube-context", argv)
            if argv[0] == "kubectl":
                self.assertEqual(argv[1], "--context")
            if argv[0] == "aws":
                self.assertIn("--region", argv)

    def test_values_file_is_not_left_behind_and_not_passed_inline(self):
        runner = self.full_runner()
        self.run_apply(runner)
        helm = next(a for a in runner.writes if a[:2] == ["helm", "upgrade"] and "dial" in a[3:4])
        path = Path(helm[helm.index("--values") + 1])
        self.assertFalse(path.exists())
        self.assertFalse(any("encryption" in part for part in helm))

    def test_dry_run_changes_nothing_and_prints_plan(self):
        runner = self.full_runner(dry_run=True)
        code, say = self.run_apply(runner, dry_run=True)
        self.assertEqual(code, 0, say.text)
        self.assertGreater(len(runner.writes), 20)
        self.assertTrue(any(a[:3] == ["aws", "cloudformation", "deploy"] for a in runner.writes))

    def test_blocking_problem_prevents_any_write(self):
        runner = FakeRunner({**aws_fakes(deployed=False),
                             ("aws", "acm", "describe-certificate"): ok_json({"Certificate": {"Status": "FAILED", "SubjectAlternativeNames": []}})})
        code, say = self.run_apply(runner)
        self.assertEqual(code, 1)
        self.assertEqual(runner.writes, [])

    def test_declining_confirmation_changes_nothing(self):
        runner = FakeRunner({**aws_fakes(deployed=False)})
        say = Capture()
        with patched_tools():
            code = workflow.cmd_apply(options(), runner, say, ask=lambda _p: "n")
        self.assertEqual(code, 0)
        self.assertEqual(runner.writes, [])
        self.assertIn("Nothing was changed", say.text)


class UpgradeTests(Base):
    def run_upgrade(self, chart, extra=None, answer="y", **kw):
        MIRRORED.clear()
        runner = FakeRunner({**aws_fakes(), **cluster_fakes(chart), **registry_fakes(), **(extra or {})})
        say = Capture()
        with patched_tools(), mock.patch.object(tools, "which", side_effect=lambda tool: f"/usr/bin/{tool}"), \
                mock.patch.object(tools, "download_text", return_value=KNATIVE_SAMPLE), mock.patch("time.sleep"):
            code = workflow.cmd_upgrade(options(**kw), runner, say, ask=lambda _p: answer)
        return code, say, runner

    def test_upgrade_to_new_chart_shows_versions_and_rollback(self):
        code, say, runner = self.run_upgrade("naya-helm-deployment-2026.8.1-old")
        self.assertEqual(code, 0, say.text)
        self.assertIn("naya-helm-deployment-2026.8.1-old", say.text)
        self.assertIn(RELEASE["chart"]["version"], say.text)
        self.assertIn("helm rollback dial 3 --namespace dial --kube-context dial-acme", say.text)
        self.assertTrue(any(a[:2] == ["helm", "upgrade"] for a in runner.writes))
        self.assertFalse(any("cloudformation" in a for a in runner.writes), "upgrade must not touch infrastructure by default")

    def rendered_live_values(self, secret_doc, mutate=None):
        """What Helm would report for a release installed from exactly this config and secret."""
        from helpers import make_ctx
        ctx = make_ctx()
        ctx.secret = secret_doc
        ctx.cache["cognito"].update(client_secret="cognito-secret", admin_client_secret="cognito-secret")
        document = json.loads(json.dumps(deploy.render(ctx)))
        if mutate:
            mutate(document)
        return document

    def live(self, secret_doc, mutate=None):
        document = self.rendered_live_values(secret_doc, mutate)
        return {("helm", "get", "values"): Result(0, json.dumps(document)),
                ("aws", "secretsmanager", "get-secret-value"): Result(0, json.dumps(secret_doc))}

    def test_unchanged_release_is_a_noop(self):
        secret_doc = secrets.generate()
        code, say, runner = self.run_upgrade(f"naya-helm-deployment-{RELEASE['chart']['version']}", extra=self.live(secret_doc))
        self.assertEqual(code, 0, say.text)
        self.assertIn("Already up to date", say.text)
        self.assertEqual(runner.writes, [kw for kw in runner.writes if kw[0] == "aws" and "update-kubeconfig" in kw])

    def test_diff_shows_changes_and_hides_secrets(self):
        secret_doc = secrets.generate()

        def mutate(document):
            document["dial"]["chat"]["env"]["APP_COGNITO_LOGOUT_URI"] = "https://old.example"
            document["dial"]["chat"]["env"]["APP_HAND_ADDED_FLAG"] = "1"      # added by hand on the live release
            document["dial"]["core"]["secrets"]["aidial.identityProviders.cognito.loggingSalt"] = "OLDSALT"

        code, say, _ = self.run_upgrade(f"naya-helm-deployment-{RELEASE['chart']['version']}",
                                        extra=self.live(secret_doc, mutate))
        self.assertEqual(code, 0, say.text)
        self.assertIn("~ dial.chat.env.APP_COGNITO_LOGOUT_URI: https://old.example -> https://chat.dial.acme.example", say.text)
        self.assertIn("- dial.chat.env.APP_HAND_ADDED_FLAG", say.text)       # a manual override that the upgrade would drop
        self.assertIn("dial.core.secrets.aidial.identityProviders.cognito.loggingSalt: (value hidden)", say.text)
        self.assertNotIn("OLDSALT", say.text)
        self.assertNotIn(secret_doc["cognito_logging_salt"], say.text)

    def test_declining_upgrade_writes_nothing(self):
        _, say, runner = self.run_upgrade("naya-helm-deployment-2026.8.1-old", answer="n")
        self.assertFalse(any(a[:2] == ["helm", "upgrade"] for a in runner.writes))
        self.assertIn("Nothing was changed", say.text)

    def test_not_installed_is_a_clear_error(self):
        from dial_install.errors import InstallerError
        runner = FakeRunner({**aws_fakes(), ("helm", "list"): Result(0, "[]")})
        with patched_tools(), self.assertRaises(InstallerError) as raised:
            workflow.cmd_upgrade(options(), runner, Capture(), ask=lambda _p: "y")
        self.assertIn("apply", raised.exception.hint)

    def test_release_not_in_package_is_read_from_the_marketplace_chart(self):
        manifest = yaml.safe_load((ROOT / "releases" / f"{RELEASE['release']}.yaml").read_text())
        manifest.update(release="2027.1.0", notes="Adds feature X.\nNo infrastructure change.")
        manifest["chart"] = {"version": "2027.1.0"}

        def pull(argv):
            target = Path(argv[argv.index("--untardir") + 1]) / "naya-helm-deployment"
            target.mkdir()
            (target / "release.yaml").write_text(yaml.safe_dump(manifest))
            return Result(0)

        code, say, runner = self.run_upgrade("naya-helm-deployment-2026.8.1-old", release="2027.1.0",
                                              extra={("helm", "pull"): pull})
        self.assertEqual(code, 0, say.text)
        helm = next(a for a in runner.writes if a[:2] == ["helm", "upgrade"])
        self.assertEqual(helm[helm.index("--version") + 1], "2027.1.0")
        self.assertIn("from registry chart", say.text)
        self.assertIn("Adds feature X.", say.text)

    def test_unknown_release_gives_a_clear_error(self):
        from dial_install.errors import InstallerError
        runner = FakeRunner({**aws_fakes(), ("helm", "pull"): Result(1, "", "not found")})
        with patched_tools(), self.assertRaises(InstallerError) as raised:
            workflow.cmd_upgrade(options(release="1999.1"), runner, Capture(), ask=lambda _p: "y")
        self.assertIn("1999.1", raised.exception.message)

    def test_older_infrastructure_blocks_upgrade_until_with_infra(self):
        from dial_install.errors import InstallerError
        import test_workflow as me
        me.INFRA_REVISION["value"] = 0
        try:
            runner = FakeRunner({**aws_fakes(), **cluster_fakes("naya-helm-deployment-2026.8.1-old")})
            with patched_tools(), self.assertRaises(InstallerError) as raised:
                workflow.cmd_upgrade(options(), runner, Capture(), ask=lambda _p: "y")
            self.assertIn("--with-infra", raised.exception.hint)
            self.assertEqual([w for w in runner.writes if w[0] == "helm"], [])
            _, _, runner = self.run_upgrade("naya-helm-deployment-2026.8.1-old", with_infra=True)
            stacks = [w for w in runner.writes if w[:3] == ["aws", "cloudformation", "deploy"]]
            self.assertTrue(all(f"dial-install:infra-revision={RELEASE['infra_revision']}" in w for w in stacks))
        finally:
            me.INFRA_REVISION["value"] = RELEASE["infra_revision"]

    def test_with_infra_deploys_stacks_first(self):
        _, _, runner = self.run_upgrade("naya-helm-deployment-2026.8.1-old", with_infra=True)
        first = runner.writes[0]
        self.assertEqual(first[:3], ["aws", "cloudformation", "deploy"])

    def test_diff_masks_exactly_the_sensitive_paths(self):
        hidden = ["a.password", "a.secrets.X", "dial.core.configuration.encryption.key", "x.INFLUX_API_TOKEN",
                  "superset.secrets.SUPERSET_METADATA_DATABASE_URI", "dial.core.secrets.aidial.config.json", "g.adminPassword"]
        shown = ["dial.chat.env.APP_COGNITO_LOGOUT_URI", "a.hosts", "ingress.alb.ingress.kubernetes.io/subnets", "monkey.name"]
        for path in hidden:
            self.assertIn("(value hidden)", deploy.diff("old", "new", path)[0], path)
        for path in shown:
            self.assertNotIn("(value hidden)", deploy.diff("old", "new", path)[0], path)

    def test_diff_masks_sensitive_paths(self):
        changes = deploy.diff({"a": {"password": "x", "region": "us"}}, {"a": {"password": "y", "region": "eu"}})
        self.assertIn("~ a.password: (value hidden)", changes)
        self.assertIn("~ a.region: us -> eu", changes)


class StatusTests(Base):
    def test_status_reports_stacks_chart_and_dns(self):
        runner = FakeRunner({**aws_fakes(), **cluster_fakes()})
        say = Capture()
        workflow.cmd_status(options(), runner, say)
        self.assertIn("acme-network", say.text)
        self.assertIn("revision 3", say.text)
        self.assertIn("alb-1.elb.amazonaws.com", say.text)


if __name__ == "__main__":
    unittest.main()


class GrantAccessTests(Base):
    def test_grants_admin_through_access_entries(self):
        runner = FakeRunner(aws_fakes())
        say = Capture()
        workflow.cmd_grant_access(options(), runner, say, "arn:aws:iam::123456789012:role/Ops")
        writes = [" ".join(w) for w in runner.writes]
        self.assertTrue(writes[0].startswith("aws eks create-access-entry --cluster-name acme-cluster --principal-arn arn:aws:iam::123456789012:role/Ops"))
        self.assertIn("AmazonEKSClusterAdminPolicy", writes[1])
        self.assertIn("aws eks update-kubeconfig", say.text)

    def test_existing_entry_is_not_an_error(self):
        runner = FakeRunner({**aws_fakes(), ("aws", "eks", "create-access-entry"): Result(254, "", "ResourceInUseException: exists")})
        workflow.cmd_grant_access(options(), runner, Capture(), "arn:aws:iam::123456789012:role/Ops")
        self.assertEqual(len(runner.writes), 2)

    def test_other_errors_are_reported(self):
        from dial_install.errors import InstallerError
        runner = FakeRunner({**aws_fakes(), ("aws", "eks", "create-access-entry"): Result(254, "", "AccessDenied")})
        with self.assertRaises(InstallerError):
            workflow.cmd_grant_access(options(), runner, Capture(), "arn:aws:iam::123456789012:role/Ops")


class CallerPrincipalTests(Base):
    """Whoever runs `apply` must get cluster access, whatever identity they use."""

    def principal(self, arn, role=None):
        responses = {("aws", "sts", "get-caller-identity"): ok_json({"Account": "123456789012", "Arn": arn})}
        if role:
            responses[("aws", "iam", "get-role")] = ok_json({"Role": {"Arn": role}})
        return make_ctx(responses=responses).caller_principal()

    def test_user_is_used_as_is(self):
        self.assertEqual(self.principal("arn:aws:iam::123456789012:user/ops"), "arn:aws:iam::123456789012:user/ops")

    def test_session_maps_to_the_role_with_its_real_path(self):
        sso = "arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/eu-west-1/AWSReservedSSO_Admin_abc"
        self.assertEqual(self.principal("arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Admin_abc/me@example.com", sso), sso)

    def test_root_user_is_skipped(self):
        self.assertIsNone(self.principal("arn:aws:iam::123456789012:root"))


class LicenseStoreTests(Base):
    """DIAL Core 0.45 needs the `apilicKey` record; the license installer of the same release only writes `licKey`."""

    def run_step(self):
        from helpers import make_ctx
        ctx = make_ctx(responses={("helm", "show", "values"): Result(0, CHART_VALUES_TEXT)})
        ctx.outputs["cache"]["RedisEndpoint"] = "rediss://cache.example.com:6379"
        deploy.ensure_api_key_license(ctx, lambda _m: None)
        return ctx.cloud.runner

    def test_record_is_created_without_overwriting(self):
        runner = self.run_step()
        call = " ".join(next(w for w in runner.writes if "run" in w and "dial-license-init" in w))
        self.assertIn("SET 6170696c69634b6579 o1Yr6cVaAt9aJf1QrhV6fg== NX", call)           # apilicKey
        self.assertIn("SET 61637469766554696572734b6579 o1Yr6cVaAt9aJf1QrhV6fg== NX", call)   # activeTiersKey
        self.assertIn("-h cache.example.com", call)
        self.assertNotIn("--rm", call)                                   # never attach to a pod that exits at once
        self.assertTrue(any("wait" in w and "pod/dial-license-init" in w for w in runner.writes))

    def test_it_runs_before_waiting_for_workloads(self):
        text = Path(workflow.__file__).read_text(encoding="utf-8")
        body = text[text.index("def _verify_and_report"):]
        self.assertLess(body.index("ensure_api_key_license"), body.index("wait_until_ready"))


class LocalChartTests(Base):
    """`--chart <dir>` (vendor) installs a chart that is not published yet, so a release is tested before it is published."""

    def test_helm_gets_the_directory_and_no_version(self):
        from helpers import make_ctx
        ctx = make_ctx(responses={("helm", "show", "values"): Result(0, CHART_VALUES_TEXT)})
        ctx.cache["local_chart"] = Path("/charts/dial-platform")
        deploy.helm_upgrade(ctx, lambda _m: None)
        call = next(w for w in ctx.cloud.runner.writes if w[0] == "helm" and "upgrade" in w)
        self.assertIn("/charts/dial-platform", call)
        self.assertNotIn("--version", call)
        self.assertNotIn("--wait", call)


    def test_a_local_chart_is_installed_even_when_its_version_is_unchanged(self):
        text = Path(workflow.__file__).read_text(encoding="utf-8")
        body = text[text.index("def cmd_upgrade"):text.index("Already up to date")]
        self.assertIn('ctx.cache.get("local_chart")', body)


class FirstUserMessageTests(Base):
    """Nobody can sign in until the first Cognito user exists; the installer says so at the end of the installation."""

    def test_the_end_of_apply_tells_how_to_create_the_first_user(self):
        from helpers import make_ctx
        ctx = make_ctx()
        say = Capture()
        workflow._print_first_user(ctx, say)
        text = say.text
        self.assertIn("create the first user", text)
        self.assertIn(FAKE_OUTPUTS["identity"]["UserPoolId"], text)
        self.assertIn("group", text)
        self.assertIn("admin-add-user-to-group", text)

    def test_with_an_identity_provider_it_names_the_admin_group_instead(self):
        from helpers import make_ctx
        cfg = resolved()
        cfg["identity"] = {**cfg["identity"], "provider": "cognito-saml"}
        say = Capture()
        workflow._print_first_user(make_ctx(cfg=cfg), say)
        self.assertIn("identity provider", say.text)
        self.assertNotIn("admin-create-user", say.text)

    def test_only_a_first_installation_prints_it(self):
        text = Path(workflow.__file__).read_text(encoding="utf-8")
        body = text[text.index("def _verify_and_report"):text.index("def _print_first_user")]
        self.assertIn("if first_install:", body)
        install = text[text.index("def _install_platform"):text.index("def _verify_and_report")]
        self.assertIn("first_install=True", install)


class LicenseHelperTests(Base):
    """dial-lic-ins seeds the license records once. An upgrade must not create it again (the Dev post-deploy and the
    customers' scripts run it once and later upgrades pass lic-ins.enabled=false)."""

    def ctx(self, release_installed, helper_exists):
        from helpers import make_ctx
        responses = {("helm", "list"): Result(0, json.dumps([{"chart": "naya-helm-deployment-2026.10.4", "revision": "2",
                                                              "status": "deployed"}]) if release_installed else "[]")}
        ctx = make_ctx(responses=responses)
        ctx.cloud.runner.responses[("kubectl", "--context", ctx.cloud.kube_context, "-n", "dial", "get", "deployment",
                                    "dial-lic-ins")] = Result(0 if helper_exists else 1, "", "")
        return ctx

    def built(self, ctx):
        from dial_install import values
        return values.build(ctx, deploy.chart_defaults(ctx))

    def test_the_first_installation_runs_the_helper(self):
        ctx = self.ctx(release_installed=False, helper_exists=False)
        self.assertTrue(deploy.license_helper_needed(ctx))
        ctx.cache["license_helper_needed"] = True
        self.assertNotIn("enabled", self.built(ctx)["lic-ins"])             # the chart default (enabled) applies

    def test_an_upgrade_does_not_create_the_helper_again(self):
        ctx = self.ctx(release_installed=True, helper_exists=False)
        self.assertFalse(deploy.license_helper_needed(ctx))
        ctx.cache["license_helper_needed"] = False
        self.assertIs(self.built(ctx)["lic-ins"]["enabled"], False)

    def test_an_unfinished_first_installation_keeps_the_helper(self):
        ctx = self.ctx(release_installed=True, helper_exists=True)
        self.assertTrue(deploy.license_helper_needed(ctx))

    def test_prepare_decides_before_the_values_are_built(self):
        text = Path(deploy.__file__).read_text(encoding="utf-8")
        body = text[text.index("def prepare"):text.index("def chart_defaults")]
        self.assertIn("license_helper_needed", body)


class LicenseReportTests(Base):
    """The installer reports how many license units the account has consumed (read only), so extra consumption is visible."""

    def ctx(self, rows):
        from helpers import make_ctx
        usage = {"LicenseUsage": {"EntitlementUsages": rows}}
        return make_ctx(responses={
            ("aws", "license-manager", "list-received-licenses"): ok_json({"Licenses": [{"LicenseArn": "arn:lic:1"}]}),
            ("aws", "license-manager", "get-license-usage"): ok_json(usage)})

    def test_consumed_units_are_listed_and_the_marketplace_meter_is_ignored(self):
        from dial_install import licenses
        ctx = self.ctx([{"Name": "small", "ConsumedValue": "14", "MaxCount": 100},
                        {"Name": "large", "ConsumedValue": "0", "MaxCount": 100},
                        {"Name": "AWS::Marketplace::Usage", "ConsumedValue": "5"}])
        rows = licenses.consumed(ctx.cloud)
        self.assertEqual(rows, [("small", "14", "100")])
        self.assertEqual(licenses.describe(rows), "small 14/100")

    def test_check_tells_how_much_is_already_consumed(self):
        from dial_install import checks
        ctx = self.ctx([{"Name": "small", "ConsumedValue": "14", "MaxCount": 100}])
        finding = checks.license_findings(ctx)[0]
        self.assertIn("small 14/100", finding.message)
        self.assertEqual(finding.level, "ok")                     # information, never a blocker

    def test_check_says_so_when_nothing_is_consumed(self):
        from dial_install import checks
        ctx = self.ctx([])
        self.assertIn("no license unit consumed", checks.license_findings(ctx)[0].message)


class AdapterNamesTests(Base):
    """DIAL Admin names its adapters with a UUID; the installer gives them the display name from installer.yaml."""

    def run_step(self, wait_fails=False):
        from helpers import make_ctx
        ctx = make_ctx(responses={("helm", "show", "values"): Result(0, CHART_VALUES_TEXT)})
        if wait_fails:
            ctx.cloud.runner.responses[("kubectl", "--context", ctx.cloud.kube_context, "-n", "dial", "wait")] = Result(1, "", "timed out")
        messages = []
        from dial_install import adapters
        adapters.name_adapters(ctx, messages.append, deploy.chart_defaults(ctx))
        return ctx.cloud.runner, messages

    def test_the_bedrock_adapter_gets_its_display_name(self):
        runner, _ = self.run_step()
        run = next(w for w in runner.writes if "run" in w and "dial-adapter-names" in w)
        overrides = json.loads(run[run.index("--overrides") + 1])
        env = {e["name"]: e["value"] for e in overrides["spec"]["containers"][0]["env"]}
        self.assertEqual(json.loads(env["WANT"]),
                         {"http://dial-bedrock.dial.svc.cluster.local/openai/deployments/": "AWS Private Bedrock"})
        self.assertEqual(env["ADMIN_URL"], "http://dial-admin-backend.dial.svc.cluster.local")
        self.assertNotIn("--rm", run)                                   # never attach to a pod that exits at once
        self.assertTrue(any("wait" in w and "pod/dial-adapter-names" in w for w in runner.writes))

    def test_no_secret_is_passed_on_the_command_line(self):
        runner, _ = self.run_step()
        run = " ".join(next(w for w in runner.writes if "run" in w and "dial-adapter-names" in w))
        self.assertIn("/mnt/core/aidial.config.json", run)              # the key is read from Core's own Secret
        self.assertNotIn("Api-Key:", run.replace('"Api-Key": key', ""))

    def test_the_script_follows_the_admin_api_contract(self):
        from dial_install import adapters
        self.assertIn("If-Match", adapters._SCRIPT)             # PUT needs the current ETag
        self.assertIn("If-None-Match", adapters._SCRIPT)        # GET of one adapter needs this header
        self.assertIn("/api/v1/adapters", adapters._SCRIPT)

    def test_a_failure_is_only_a_warning(self):
        _, messages = self.run_step(wait_fails=True)
        self.assertTrue(any("warning" in m and "adapter" in m for m in messages), messages)

    def test_it_runs_after_the_workloads_are_ready(self):
        text = Path(workflow.__file__).read_text(encoding="utf-8")
        body = text[text.index("def _verify_and_report"):]
        self.assertLess(body.index("wait_until_ready"), body.index("name_adapters"))


class MirrorCommandTests(Base):
    """`mirror` copies only the images, so it can run in CloudShell while the infrastructure is being built."""

    def test_copies_images_and_creates_nothing_else(self):
        runner = FakeRunner({**aws_fakes(deployed=False), ("helm", "show", "values"): Result(0, CHART_VALUES_TEXT)})
        say = Capture()
        with patched_tools(), mock.patch("dial_install.mirror.copy_images", return_value=(3, 40)) as copy:
            code = workflow.cmd_mirror(options(), runner, say)
        self.assertEqual(code, 0, say.text)
        copy.assert_called_once()
        self.assertFalse(runner.called("aws", "cloudformation", "deploy"))
        self.assertIn("Images are in your registry", say.text)

    def test_public_registry_has_nothing_to_copy(self):
        runner = FakeRunner({**aws_fakes(deployed=False)})
        say = Capture()
        opts = options()
        with patched_tools(), mock.patch.object(workflow.config, "load", wraps=workflow.config.load) as load:
            raw = workflow.config.load(opts.config_path)
        raw["registry"] = {"provider": "public"}
        with patched_tools(), mock.patch.object(workflow.config, "load", return_value=raw):
            code = workflow.cmd_mirror(opts, runner, say)
        self.assertEqual(code, 0)
        self.assertIn("nothing to copy", say.text)


class UninstallTests(Base):
    """`uninstall` removes only what the installation owns, in an order that cannot get stuck."""

    def run_uninstall(self, extra=None, **kw):
        stack = {"Stacks": [{"StackStatus": "CREATE_COMPLETE", "Tags": [{"Key": "dial-install:infra-revision", "Value": "1"}],
                             "Outputs": [{"OutputKey": "VPCId", "OutputValue": "vpc-0a1"}]}]}
        runner = FakeRunner({**aws_fakes(), ("aws", "cloudformation", "describe-stacks"): ok_json(stack),
                             ("aws", "cloudformation", "describe-stack-resources"): ok_json({"StackResources": [
                                 {"ResourceType": "AWS::RDS::DBCluster", "PhysicalResourceId": "acme-db"},
                                 {"ResourceType": "AWS::S3::Bucket", "PhysicalResourceId": "acme-bucket"},
                                 {"ResourceType": "AWS::ECR::Repository", "PhysicalResourceId": "aidial/code-app"}]}),
                             ("aws", "ecr", "describe-repositories"): ok_json({"repositories": []}),
                             ("aws", "license-manager", "list-received-licenses"): ok_json({"Licenses": []}),
                             **(extra or {})})
        say = Capture()
        code = workflow.cmd_uninstall(options(**kw), runner, say, ask=lambda _q: "acme")
        return code, say.text, runner

    def writes(self, runner):
        return [" ".join(w) for w in runner.writes]

    def test_asks_for_the_name_and_cancels_on_a_wrong_answer(self):
        runner = FakeRunner({**aws_fakes()})
        say = Capture()
        code = workflow.cmd_uninstall(options(), runner, say, ask=lambda _q: "something else")
        self.assertEqual(code, 0)
        self.assertEqual(runner.writes, [])
        self.assertIn("Nothing was changed", say.text)

    def test_load_balancers_go_before_the_application(self):
        _, _, runner = self.run_uninstall(assume_yes=True)
        writes = self.writes(runner)
        ingress = next(i for i, w in enumerate(writes) if "delete ingress" in w)
        helm = next(i for i, w in enumerate(writes) if "uninstall dial" in w)
        stacks = next(i for i, w in enumerate(writes) if "delete-stack" in w)
        self.assertLess(ingress, helm)
        self.assertLess(helm, stacks)

    def test_network_and_storage_are_deleted_last(self):
        _, _, runner = self.run_uninstall(assume_yes=True)
        order = [w.split("--stack-name ")[1].split()[0] for w in self.writes(runner) if "delete-stack" in w]
        self.assertEqual(order[-3:], ["acme-endpoints", "acme-network", "acme-storage"])
        self.assertIn("acme-compute", order[:-3])

    def test_security_groups_left_by_the_load_balancer_controller_go_before_the_network(self):
        groups = {"SecurityGroups": [{"GroupId": "sg-1", "GroupName": "k8s-traffic-acme"},
                                     {"GroupId": "sg-2", "GroupName": "default"}]}
        _, _, runner = self.run_uninstall(extra={("aws", "ec2", "describe-security-groups"): ok_json(groups)}, assume_yes=True)
        writes = self.writes(runner)
        group = next(i for i, w in enumerate(writes) if "delete-security-group --group-id sg-1" in w)
        network = next(i for i, w in enumerate(writes) if "delete-stack --stack-name acme-network" in w)
        self.assertLess(group, network)
        self.assertFalse(any("sg-2" in w for w in writes))

    def test_the_versioned_bucket_is_emptied_of_versions_and_delete_markers_before_its_stack_goes(self):
        pages = [{"Versions": [{"Key": "a", "VersionId": "v1"}], "DeleteMarkers": [{"Key": "b", "VersionId": "m1"}]}, {}]

        def listing(_argv):
            return Result(0, json.dumps(pages.pop(0) if pages else {}), "")

        _, _, runner = self.run_uninstall(extra={("aws", "s3api", "list-object-versions"): listing}, assume_yes=True)
        writes = self.writes(runner)
        delete = next(i for i, w in enumerate(writes) if "s3api delete-objects --bucket acme-bucket" in w)
        self.assertIn('"VersionId": "v1"', writes[delete])
        self.assertIn('"VersionId": "m1"', writes[delete])
        stack = next(i for i, w in enumerate(writes) if "delete-stack --stack-name acme-storage" in w)
        self.assertLess(delete, stack)

    def test_database_protection_is_switched_off_before_its_stack_is_deleted(self):
        _, _, runner = self.run_uninstall(assume_yes=True)
        writes = self.writes(runner)
        off = next(i for i, w in enumerate(writes) if "--no-deletion-protection" in w)
        delete = next(i for i, w in enumerate(writes) if "delete-stack --stack-name acme-database" in w)
        self.assertLess(off, delete)

    def test_keep_flags_are_respected(self):
        _, _, runner = self.run_uninstall(assume_yes=True, keep_secret=True, keep_images=True, keep_snapshot=True)
        text = " ".join(self.writes(runner))
        self.assertNotIn("delete-secret", text)
        self.assertNotIn("delete-db-cluster-snapshot", text)

    def test_stack_without_the_installer_tag_is_left_alone(self):
        stack = {"Stacks": [{"StackStatus": "CREATE_COMPLETE", "Tags": []}]}
        runner = FakeRunner({**aws_fakes(), ("aws", "cloudformation", "describe-stacks"): ok_json(stack)})
        workflow.cmd_uninstall(options(assume_yes=True), runner, Capture(), ask=lambda _q: "acme")
        self.assertFalse(any("delete-stack" in w for w in self.writes(runner)))

    def test_dry_run_changes_nothing_and_needs_no_question(self):
        asked = []
        runner = FakeRunner({**aws_fakes()}, dry_run=True)
        workflow.cmd_uninstall(options(dry_run=True), runner, Capture(), ask=lambda q: asked.append(q) or "")
        self.assertEqual(asked, [])
