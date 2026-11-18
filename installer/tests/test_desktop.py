"""DIAL Desktop support: the Cognito app client, the chat setting, the flag, and nothing changing when it is off."""

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from helpers import Base, CHART_VALUES_TEXT, FAKE_OUTPUTS, PLATFORM_CHART, make_ctx, resolved
from dial_install import desktop, modules, values
from dial_install.errors import InstallerError
from dial_install.values import placeholders
from test_modules import template

HAVE_HELM = subprocess.run(["which", "helm"], capture_output=True).returncode == 0
CLIENT = "desktopclient123"


def identity_module(cfg):
    return next(m for m in modules.select(cfg) if m.slot == "identity")


def ctx_with(enabled, client=CLIENT, provider="cognito"):
    cfg = resolved(features={"desktop_app": enabled}) if provider == "cognito" else resolved(
        features={"desktop_app": enabled}, identity={"provider": "cognito-saml", "saml": {
            "provider_name": "Corp", "metadata_url": "https://idp.example.org/m.xml", "idp_signout": False}})
    ctx = make_ctx(cfg=cfg)
    ctx.outputs["identity"] = {**copy.deepcopy(FAKE_OUTPUTS["identity"]), "DesktopClientId": client if enabled else ""}
    return ctx


class StackTests(Base):
    def setUp(self):
        self.module = identity_module(resolved())
        self.template = template(self.module)
        self.client = self.template["Resources"]["DesktopAppClient"]["Properties"]

    def test_the_client_is_only_created_when_asked_for(self):
        self.assertEqual(self.template["Resources"]["DesktopAppClient"]["Condition"], "DesktopAppEnabled")
        self.assertEqual(self.template["Parameters"]["EnableDesktopApp"]["Default"], "false")
        self.assertEqual(self.template["Outputs"]["DesktopClientId"]["Condition"], "DesktopAppEnabled")

    def test_it_is_a_public_client_with_the_authorization_code_flow_only(self):
        self.assertEqual(self.client["ClientName"], "dial-x-desktop")
        self.assertIs(self.client["GenerateSecret"], False)
        self.assertEqual(self.client["AllowedOAuthFlows"], ["code"])
        self.assertIs(self.client["AllowedOAuthFlowsUserPoolClient"], True)
        self.assertEqual(sorted(self.client["AllowedOAuthScopes"]), ["email", "openid", "profile"])

    def test_the_urls_are_the_custom_scheme_of_the_application(self):
        self.assertEqual(self.client["CallbackURLs"], ["nayadial://auth/callback"])
        self.assertEqual(self.client["LogoutURLs"], ["nayadial://auth/logout"])

    def test_it_allows_the_same_identity_providers_as_the_chat_client(self):
        chat = self.template["Resources"]["ChatAppClient"]["Properties"]
        self.assertEqual(self.client["SupportedIdentityProviders"], chat["SupportedIdentityProviders"])

    def test_tokens(self):
        chat = self.template["Resources"]["ChatAppClient"]["Properties"]
        self.assertEqual(self.client["RefreshTokenValidity"], 30)
        self.assertEqual(self.client["TokenValidityUnits"], chat["TokenValidityUnits"])
        for key in ("AccessTokenValidity", "IdTokenValidity", "EnableTokenRevocation", "PreventUserExistenceErrors"):
            self.assertEqual(self.client[key], chat[key], key)

    def test_the_chat_client_is_unchanged(self):
        chat = self.template["Resources"]["ChatAppClient"]["Properties"]
        self.assertIs(chat["GenerateSecret"], True)
        self.assertEqual(chat["RefreshTokenValidity"], 5)
        self.assertNotIn("nayadial://auth/callback", json.dumps(chat))

    def test_the_stack_gets_the_flag(self):
        for enabled in (False, True):
            cfg = resolved(features={"desktop_app": enabled})
            ctx = make_ctx(cfg=cfg)
            self.assertEqual(identity_module(cfg).parameters(ctx)["EnableDesktopApp"], str(enabled).lower())

    def test_the_module_promises_the_output_and_returns_it_empty_when_off(self):
        self.assertIn("DesktopClientId", self.module.provides)
        ctx = make_ctx()
        self.assertEqual(self.module.finalize_outputs(ctx, {"ClientId": "x"})["DesktopClientId"], "")
        self.assertEqual(self.module.finalize_outputs(ctx, {"DesktopClientId": "abc"})["DesktopClientId"], "abc")


class FlagTests(Base):
    def test_off_by_default(self):
        self.assertFalse(resolved()["features"]["desktop_app"])

    def test_a_wrong_value_is_refused_by_the_schema(self):
        from dial_install import config, schema
        from helpers import example
        raw = example()
        raw["features"] = {"desktop_app": "yes"}
        self.assertTrue(schema.validate(raw, config.load_schema()))
        raw["features"] = {"desktop_app": True}
        self.assertEqual(schema.validate(raw, config.load_schema()), [])


class ChatSettingTests(Base):
    def chart_values(self):
        return yaml.safe_load(CHART_VALUES_TEXT)

    def test_the_chart_declares_the_placeholder_and_the_installer_knows_it(self):
        text = CHART_VALUES_TEXT
        self.assertIn("APP_COGNITO_DESKTOP_CLIENT_ID: 'PLACEHOLDER_COGNITO_DESKTOP_CLIENT_ID'", text)
        self.assertIn("COGNITO_DESKTOP_CLIENT_ID", placeholders.table(make_ctx()))

    def test_the_chat_gets_the_client_id_when_enabled(self):
        built = values.build(ctx_with(True), self.chart_values())
        self.assertEqual(built["dial"]["chat"]["env"]["APP_COGNITO_DESKTOP_CLIENT_ID"], CLIENT)

    def test_the_chat_gets_an_empty_value_when_disabled(self):
        built = values.build(ctx_with(False), self.chart_values())
        self.assertEqual(built["dial"]["chat"]["env"]["APP_COGNITO_DESKTOP_CLIENT_ID"], "")

    def test_the_existing_client_id_of_the_chat_is_not_touched(self):
        built = values.build(ctx_with(True), self.chart_values())
        self.assertEqual(built["dial"]["chat"]["env"]["APP_COGNITO_CLIENT_ID"], FAKE_OUTPUTS["identity"]["ClientId"])

    def test_enabled_without_the_client_stops_with_what_to_do(self):
        ctx = ctx_with(True, client="")
        with self.assertRaises(InstallerError) as raised:
            values.build(ctx, self.chart_values())
        self.assertIn("upgrade --with-infra", raised.exception.hint)

    def test_it_works_with_saml_too(self):
        built = values.build(ctx_with(True, provider="saml"), self.chart_values())
        self.assertEqual(built["dial"]["chat"]["env"]["APP_COGNITO_DESKTOP_CLIENT_ID"], CLIENT)

    def test_core_and_the_other_services_need_no_change(self):
        on, off = ctx_with(True), ctx_with(False)
        off.secret = on.secret                                   # the generated secrets differ per context otherwise
        enabled = values.build(on, self.chart_values())
        disabled = values.build(off, self.chart_values())
        del enabled["dial"]["chat"]["env"]["APP_COGNITO_DESKTOP_CLIENT_ID"]
        del disabled["dial"]["chat"]["env"]["APP_COGNITO_DESKTOP_CLIENT_ID"]
        self.assertEqual(enabled, disabled)

    @unittest.skipUnless(HAVE_HELM, "helm is not installed")
    def test_the_rendered_chat_deployment_carries_it(self):
        for enabled, expected in ((True, CLIENT), (False, "")):
            with tempfile.NamedTemporaryFile("w", suffix=".yaml") as handle:
                yaml.safe_dump(values.build(ctx_with(enabled), self.chart_values()), handle)
                handle.flush()
                result = subprocess.run(["helm", "template", "dial", str(PLATFORM_CHART), "-n", "dial", "-f", handle.name],
                                        capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            chat = next(d for d in yaml.safe_load_all(result.stdout) if d and d["kind"] == "Deployment" and d["metadata"]["name"] == "dial-chat")
            env = {e["name"]: e.get("value") for e in chat["spec"]["template"]["spec"]["containers"][0]["env"]}
            self.assertEqual(env["APP_COGNITO_DESKTOP_CLIENT_ID"], expected)


CHAT_CONFIG = 'window.__DIAL_CONFIG__ = {"desktop":{"cognitoClientId":"%s"}};' % CLIENT


class Site:
    """A scripted fake of the internet: which address answers what."""

    def __init__(self, config_js=CHAT_CONFIG, authorize=200, missing_dns=(), refuse=()):
        self.config_js, self.authorize, self.missing_dns, self.refuse = config_js, authorize, set(missing_dns), set(refuse)
        self.asked = []

    def __call__(self, url):
        from urllib.parse import urlparse
        host = urlparse(url).hostname
        self.asked.append(url)
        if host in self.missing_dns:
            raise desktop.NotResolvable(host)
        if host in self.refuse:
            raise desktop.Unreachable("connection refused")
        if "/oauth2/authorize" in url:
            return self.authorize, ""
        if url.endswith("/config.js"):
            return 200, self.config_js
        return 200, "<html>"


def desktop_ctx():
    ctx = ctx_with(True)
    ctx.cache["output_dir"] = None
    return ctx


class ConnectionFileTests(Base):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.directory = Path(self.dir.name)
        self.ctx = ctx_with(True)
        self.lines = []

    def deliver(self, site=None, ctx=None, chat_id="managed-model-chat"):
        return desktop.deliver(ctx or self.ctx, self.lines.append, self.directory, chat_id, site or Site())

    @property
    def text(self):
        return "\n".join(self.lines)

    def test_the_file_holds_exactly_the_documented_fields_from_known_values(self):
        path = self.deliver()
        self.assertEqual(path, (self.directory / "acme.dialx.json").resolve())
        self.assertEqual(json.loads(path.read_text()), {
            "name": "acme",
            "coreUrl": "https://core.dial.acme.example", "chatUrl": "https://chat.dial.acme.example",
            "adminUrl": "https://admin.dial.acme.example", "flowsUrl": "https://flows.dial.acme.example",
            "cognito": {"region": "us-east-1", "userPoolId": "us-east-1_pool",
                        "domain": "https://acme.auth.us-east-1.amazoncognito.com", "clientId": CLIENT},
            "managedChatApplicationId": "managed-model-chat"})

    def test_custom_host_names_are_used(self):
        cfg = resolved(features={"desktop_app": True}, domain={"hosts": {"chat": "talk.example.org"}})
        ctx = make_ctx(cfg=cfg)
        ctx.outputs["identity"] = {**FAKE_OUTPUTS["identity"], "DesktopClientId": CLIENT}
        self.assertEqual(json.loads(self.deliver(ctx=ctx).read_text())["chatUrl"], "https://talk.example.org")

    def test_it_prints_the_path_the_content_and_the_message(self):
        path = self.deliver()
        self.assertIn("Environment acme is ready.", self.text)
        self.assertIn(f"Desktop connection file: {path}", self.text)
        self.assertIn("New connection > Import file > choose the file > Save, then click the connection to sign in.", self.text)
        self.assertIn('"userPoolId": "us-east-1_pool"', self.text)
        self.assertIn("do not publish", self.text)

    def test_a_new_run_replaces_the_old_file(self):
        self.deliver()
        (self.directory / "acme.dialx.json").write_text("old")
        self.deliver()
        self.assertIn("coreUrl", (self.directory / "acme.dialx.json").read_text())

    def test_nothing_is_written_when_desktop_is_not_supported_or_in_a_dry_run(self):
        off = ctx_with(False)
        self.assertIsNone(self.deliver(ctx=off))
        dry = make_ctx(cfg=resolved(features={"desktop_app": True}), dry_run=True)
        self.assertIsNone(self.deliver(ctx=dry))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_every_check_has_a_name_and_passes_for_a_healthy_environment(self):
        self.deliver()
        for name in ("file-format", "urls-https", "no-secrets", "servers-answer", "chat-config-has-client", "cognito-authorize"):
            self.assertIn(f"[ OK ] {name}", self.text)

    def test_the_authorize_request_uses_the_client_and_the_callback_of_the_application(self):
        site = Site()
        self.deliver(site)
        url = next(u for u in site.asked if "/oauth2/authorize" in u)
        self.assertTrue(url.startswith("https://acme.auth.us-east-1.amazoncognito.com/oauth2/authorize?"))
        for part in (f"client_id={CLIENT}", "response_type=code", "scope=openid+email+profile", "redirect_uri=nayadial%3A%2F%2Fauth%2Fcallback"):
            self.assertIn(part, url)

    def failure(self, site):
        with self.assertRaises(InstallerError) as raised:
            self.deliver(site)
        self.assertFalse((self.directory / "acme.dialx.json").exists(), "a file that failed its checks must not be left behind")
        return raised.exception

    def test_a_chat_that_has_not_picked_up_the_setting_fails_with_that_check(self):
        error = self.failure(Site(config_js='window.__DIAL_CONFIG__ = {"desktop":{"cognitoClientId":""}};'))
        self.assertIn("chat-config-has-client", error.message)

    def test_a_chat_without_any_desktop_block_fails_too(self):
        self.assertIn("chat-config-has-client", self.failure(Site(config_js="window.__DIAL_CONFIG__ = {};")).message)

    def test_cognito_refusing_the_client_fails_with_that_check_and_names_the_likely_cause(self):
        error = self.failure(Site(authorize=403))
        self.assertIn("cognito-authorize", error.message)
        self.assertIn("managed-login style", error.hint)

    def test_a_server_that_does_not_answer_fails(self):
        self.assertIn("servers-answer", self.failure(Site(refuse={"core.dial.acme.example"})).message)

    def test_missing_dns_records_wait_instead_of_failing_and_say_how_to_check_again(self):
        site = Site(missing_dns={"chat.dial.acme.example", "core.dial.acme.example", "admin.dial.acme.example", "flows.dial.acme.example"})
        path = self.deliver(site)
        self.assertTrue(path.exists())
        self.assertIn("[WAIT] servers-answer", self.text)
        self.assertIn("[WAIT] chat-config-has-client", self.text)
        self.assertIn("[ OK ] cognito-authorize", self.text)       # Cognito does not depend on the customer's DNS
        self.assertIn("./dial-install status", self.text)

    def test_checks_on_the_text_itself(self):
        doc = desktop.document(self.ctx, "managed-model-chat")
        good = json.dumps(doc)

        def status(text, name):
            return next(c for c in desktop.verify(self.ctx, doc, text, Site()) if c.name == name).status
        self.assertEqual(status(good, "file-format"), "ok")
        self.assertEqual(status("{not json", "file-format"), "fail")
        self.assertEqual(status(json.dumps({**doc, "padding": "x" * 70000}), "file-format"), "fail")
        self.assertEqual(status(json.dumps({**doc, "n": 1}), "file-format"), "fail")        # every value must be text
        self.assertEqual(status(good, "no-secrets"), "ok")
        self.assertEqual(status(good + self.ctx.secret["dial_api_key"], "no-secrets"), "fail")
        self.assertEqual(status(good + "client " + self.ctx.cache["cognito"]["client_secret"] + "xxxxx", "no-secrets"), "ok")   # too short to be a secret
        self.assertEqual(status(good.replace('"name"', '"apiKey"'), "no-secrets"), "fail")
        self.assertEqual(status(good + "eyJhbGciOiJIUzI1NiJ9.payload", "no-secrets"), "fail")
        http = dict(doc, chatUrl="http://chat.dial.acme.example")
        self.assertEqual(next(c for c in desktop.verify(self.ctx, http, json.dumps(http), Site()) if c.name == "urls-https").status, "fail")

    def test_the_file_has_no_secret_in_a_real_run(self):
        path = self.deliver()
        for value in self.ctx.secret.values():
            self.assertNotIn(value, path.read_text())


class InstallFlowTests(Base):
    def test_apply_writes_the_file_next_to_the_configuration_and_a_second_run_replaces_it(self):
        import test_workflow
        from unittest import mock as _mock
        raw = yaml.safe_load((test_workflow.ROOT / "examples" / "dial.minimal.yaml").read_text())
        raw["features"] = {"desktop_app": True}
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "dial.yaml"
            config.write_text(yaml.safe_dump(raw))
            original = copy.deepcopy(FAKE_OUTPUTS["identity"])
            FAKE_OUTPUTS["identity"]["DesktopClientId"] = CLIENT
            self.addCleanup(lambda: FAKE_OUTPUTS.__setitem__("identity", original))
            with _mock.patch.object(desktop, "fetch", Site()):
                runner, code, say = test_workflow.run_full_apply(config)
                self.assertEqual(code, 0, say.text)
                path = Path(directory) / "acme.dialx.json"
                self.assertTrue(path.is_file(), say.text)
                self.assertEqual(json.loads(path.read_text())["cognito"]["clientId"], CLIENT)
                self.assertEqual(json.loads(path.read_text())["managedChatApplicationId"], "managed-model-chat")
                self.assertIn("Environment acme is ready.", say.text)
                path.write_text("stale")
                _, code, say = test_workflow.run_full_apply(config)
                self.assertEqual(code, 0, say.text)
                self.assertIn("coreUrl", path.read_text())
            deployed = [a for a in runner.writes if a[:3] == ["aws", "cloudformation", "deploy"] and "acme-identity" in a]
            self.assertIn("EnableDesktopApp=true", deployed[0])

    def test_without_the_flag_apply_writes_no_file(self):
        import test_workflow
        raw = yaml.safe_load((test_workflow.ROOT / "examples" / "dial.minimal.yaml").read_text())
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "dial.yaml"
            config.write_text(yaml.safe_dump(raw))
            _, code, say = test_workflow.run_full_apply(config)
            self.assertEqual(code, 0, say.text)
            self.assertEqual([p.name for p in Path(directory).iterdir()], ["dial.yaml"])
            self.assertNotIn("Desktop connection file", say.text)


if __name__ == "__main__":
    unittest.main()
