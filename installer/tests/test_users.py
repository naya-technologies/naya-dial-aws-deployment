"""DIAL groups (permission levels), the first administrator, and the summary at the end of an installation."""

import json
import subprocess
import tempfile
import unittest

import yaml

from helpers import Base, CHART_VALUES_TEXT, PLATFORM_CHART, make_ctx, ok_json, resolved
from dial_install import config, users, values
from dial_install.errors import InstallerError
from dial_install.runner import Result

HAVE_HELM = subprocess.run(["which", "helm"], capture_output=True).returncode == 0
ADMIN = "admin@dial.acme.example"


class Pool:
    """A scripted Cognito user pool."""

    def __init__(self, groups=(), members=(), users_=None):
        self.groups = set(groups)
        self.members = {name: list(m) for name, m in dict(members).items()}
        self.users = dict(users_ or {})                # username -> status
        self.events = []

    def responses(self, fail_create=False):
        return {
            ("aws", "cognito-idp", "list-groups"): lambda argv: ok_json({"Groups": [{"GroupName": g} for g in sorted(self.groups)]}),
            ("aws", "cognito-idp", "create-group"): self.create_group,
            ("aws", "cognito-idp", "list-users-in-group"): self.list_members,
            ("aws", "cognito-idp", "admin-get-user"): self.get_user,
            ("aws", "cognito-idp", "admin-create-user"): Result(1, "", "InvalidPasswordException") if fail_create else self.create_user,
            ("aws", "cognito-idp", "admin-add-user-to-group"): self.add,
        }

    def arg(self, argv, name):
        return argv[argv.index(name) + 1]

    def create_group(self, argv):
        self.groups.add(self.arg(argv, "--group-name"))
        self.events.append(("create-group", self.arg(argv, "--group-name"), self.arg(argv, "--precedence")))
        return Result(0)

    def list_members(self, argv):
        group = self.arg(argv, "--group-name")
        return ok_json({"Users": [{"Username": u, "UserStatus": self.users.get(u, "CONFIRMED"),
                                   "Attributes": [{"Name": "email", "Value": u}]} for u in self.members.get(group, [])]})

    def get_user(self, argv):
        name = self.arg(argv, "--username")
        return ok_json({"Username": name, "UserStatus": self.users[name]}) if name in self.users else Result(254, "", "UserNotFoundException")

    def create_user(self, argv):
        name = self.arg(argv, "--username")
        self.users[name] = "FORCE_CHANGE_PASSWORD"
        self.events.append(("create-user", name))
        self.created = argv
        return Result(0)

    def add(self, argv):
        self.members.setdefault(self.arg(argv, "--group-name"), []).append(self.arg(argv, "--username"))
        self.events.append(("add", self.arg(argv, "--username"), self.arg(argv, "--group-name")))
        return Result(0)


def run(pool, cfg=None, **kw):
    ctx = make_ctx(cfg=cfg, responses=pool.responses(**kw))
    return ctx, users.prepare(ctx, lambda _m: None)


class GroupTests(Base):
    def test_the_three_groups_are_created_with_their_precedence(self):
        pool = Pool()
        run(pool)
        self.assertEqual(sorted(e for e in pool.events if e[0] == "create-group"),
                         [("create-group", "admin", "1"), ("create-group", "developer", "2"), ("create-group", "user", "3")])

    def test_names_come_from_the_configuration_and_existing_groups_are_kept(self):
        pool = Pool(groups={"dial-admins"})
        run(pool, cfg=resolved(identity={"groups": {"user": "dial-users", "developer": "dial-devs", "admin": "dial-admins"}}))
        self.assertEqual({e[1] for e in pool.events if e[0] == "create-group"}, {"dial-users", "dial-devs"})

    def test_three_different_names_are_required(self):
        problems = config.semantic_errors(resolved(identity={"groups": {"user": "x", "developer": "x", "admin": "a"}}))
        self.assertTrue(any("three different names" in p for p in problems))


class FirstAdministratorTests(Base):
    def test_created_with_the_initial_password_and_put_in_the_admin_group(self):
        pool = Pool()
        _, account = run(pool)
        self.assertEqual(account, {"username": ADMIN, "password": "DialXWelcome123!", "created": True})
        argv = pool.created
        self.assertEqual(argv[argv.index("--temporary-password") + 1], "DialXWelcome123!")
        self.assertEqual(argv[argv.index("--message-action") + 1], "SUPPRESS")                # no e-mail to an address that may not exist
        self.assertIn("Name=email_verified,Value=true", argv)
        self.assertIn(("add", ADMIN, "admin"), pool.events)

    def test_the_admin_group_follows_its_configured_name(self):
        pool = Pool()
        run(pool, cfg=resolved(identity={"groups": {"user": "u", "developer": "d", "admin": "dial-admins"}}))
        self.assertIn(("add", ADMIN, "dial-admins"), pool.events)

    def test_a_second_run_creates_nothing_and_shows_the_password_until_it_is_changed(self):
        pool = Pool(groups={"user", "developer", "admin"}, members={"admin": [ADMIN]}, users_={ADMIN: "FORCE_CHANGE_PASSWORD"})
        _, account = run(pool)
        self.assertEqual(pool.events, [])
        self.assertEqual(account["password"], "DialXWelcome123!")
        pool.users[ADMIN] = "CONFIRMED"
        _, account = run(pool)
        self.assertIsNone(account["password"])

    def test_when_the_customer_has_administrators_nothing_is_created(self):
        pool = Pool(groups={"user", "developer", "admin"}, members={"admin": ["boss@corp.example"]})
        _, account = run(pool)
        self.assertEqual(pool.events, [])
        self.assertIsNone(account["username"])

    def test_an_existing_user_is_put_back_in_the_group_not_recreated(self):
        pool = Pool(groups={"user", "developer", "admin"}, users_={ADMIN: "CONFIRMED"})
        _, account = run(pool)
        self.assertNotIn(("create-user", ADMIN), pool.events)
        self.assertIn(("add", ADMIN, "admin"), pool.events)
        self.assertIsNone(account["password"])

    def test_a_failure_is_explained(self):
        with self.assertRaises(InstallerError) as raised:
            run(Pool(), fail_create=True)
        self.assertIn("AdminCreateUser", raised.exception.hint)

    def test_a_dry_run_changes_nothing(self):
        pool = Pool()
        ctx = make_ctx(responses=pool.responses(), dry_run=True)
        self.assertIsNone(users.prepare(ctx, lambda _m: None))
        self.assertEqual(pool.events, [])


class SummaryTests(Base):
    RECORDS = {"chat.dial.acme.example": "k8s-alb-1.elb.amazonaws.com", "admin.dial.acme.example": "k8s-alb-1.elb.amazonaws.com"}

    def test_interfaces_dns_and_the_sign_in_in_plain_text(self):
        ctx = make_ctx()
        text = users.summary(ctx, self.RECORDS, {"username": ADMIN, "password": "DialXWelcome123!", "created": True})
        for url in ("https://chat.dial.acme.example", "https://admin.dial.acme.example", "https://flows.dial.acme.example",
                    "https://core.dial.acme.example", "https://grafana.dial.acme.example", "https://superset.dial.acme.example"):
            self.assertIn(url, text)
        self.assertRegex(text, r"chat\.dial\.acme\.example +->  k8s-alb-1\.elb\.amazonaws\.com")
        self.assertIn(f"User name   {ADMIN}", text)
        self.assertIn("Password    DialXWelcome123!", text)
        self.assertIn("new password at the first sign-in", text)
        self.assertIn("user (user), developer (developer), admin (admin)", text)

    def test_without_analytics_and_after_the_password_changed(self):
        ctx = make_ctx(cfg=resolved(features={"analytics": False}))
        text = users.summary(ctx, {}, {"username": ADMIN, "password": None, "created": False})
        self.assertNotIn("grafana", text)
        self.assertNotIn("DialXWelcome123!", text)
        self.assertIn("status in a few minutes", text)


class ChartTests(Base):
    CUSTOM = {"user": "dial-users", "developer": "dial-devs", "admin": "dial-admins"}

    def built(self):
        ctx = make_ctx(cfg=resolved(identity={"groups": self.CUSTOM}))
        return ctx, values.build(ctx, yaml.safe_load(CHART_VALUES_TEXT))

    def test_the_admin_group_is_the_administrator_everywhere(self):
        _, built = self.built()
        text = json.dumps(built)
        self.assertEqual(built["admin"]["frontend"]["env"]["ADMIN_ROLE_NAMES"], "dial-admins")
        self.assertEqual(built["admin"]["frontend"]["env"]["AUTH_COGNITO_ADMIN_ROLE_NAMES"], "dial-admins")
        self.assertEqual(built["flows-core"]["env"]["ADMIN_ROLE_NAMES"], "dial-admins")
        manager = json.loads(built["deployment-manager"]["env"]["SPRING_APPLICATION_JSON"])
        self.assertEqual(json.loads(manager["providers"]["cognito"]["rolesMapping"])["dial-admins"], ["FULL_ADMIN"])   # Deployment Manager
        self.assertNotIn("PLACEHOLDER_", text)

    def test_core_knows_every_group_and_the_chat_application_allows_them(self):
        _, built = self.built()
        roles = json.loads(built["dial"]["core"]["secrets"]["aidial.config.json"])["roles"]
        self.assertTrue({"admin", "default", "dial-users", "dial-devs", "dial-admins"} <= set(roles))
        chat_app = built["dial"]["core"]["extraDeploy"][0]["data"]["managed-model-chat.json"]
        self.assertIn('"userRoles": ["default", "dial-users", "dial-devs", "dial-admins"]', chat_app)

    @unittest.skipUnless(HAVE_HELM, "helm is not installed")
    def test_the_chart_renders_with_the_group_names(self):
        _, built = self.built()
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as handle:
            yaml.safe_dump(built, handle)
            handle.flush()
            result = subprocess.run(["helm", "template", "dial", str(PLATFORM_CHART), "-n", "dial", "-f", handle.name], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dial-admins", result.stdout)
