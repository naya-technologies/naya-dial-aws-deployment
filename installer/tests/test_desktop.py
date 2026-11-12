"""DIAL Desktop support: the Cognito app client, the chat setting, the flag, and nothing changing when it is off."""

import copy
import json
import subprocess
import tempfile
import unittest

import yaml

from helpers import Base, CHART_VALUES_TEXT, FAKE_OUTPUTS, PLATFORM_CHART, make_ctx, resolved
from dial_install import modules, values
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


if __name__ == "__main__":
    unittest.main()
