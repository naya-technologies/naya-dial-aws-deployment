"""identity/cognito and identity/cognito-saml."""

import urllib.error
import urllib.request

from dial_install.findings import fail, ok, warn


def check(ctx):
    identity = ctx.cfg["identity"]
    if identity["provider"] != "cognito-saml":
        return [ok("identity", "Cognito-managed users (no external identity provider)")]
    url = identity["saml"]["metadata_url"]
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 (https enforced by schema)
            status = response.status
    except urllib.error.HTTPError as error:
        return [fail("identity", f"SAML metadata URL answered HTTP {error.code}: {url}",
                     "Cognito downloads this URL itself, so it must be reachable from the internet.")]
    except (urllib.error.URLError, TimeoutError) as error:
        return [warn("identity", f"could not reach the SAML metadata URL from this machine ({error})",
                     "Cognito downloads it from AWS, so it must be reachable from the internet; "
                     "this machine may simply be on a different network.")]
    return [ok("identity", f"SAML metadata reachable (HTTP {status})")]


def parameters(ctx):
    identity = ctx.cfg["identity"]
    hosts = ctx.cfg["domain"]["hosts"]
    params = {
        "UserPoolName": identity["user_pool_name"],
        "ChatHost": hosts["chat"],
        "AdminHost": hosts["admin"],
        "FlowsHost": hosts["flows"],
        "FlowsCoreHost": hosts["flows-core"],
        "DisableSelfRegistration": str(not identity["self_registration"]).lower(),
        "HostedUIDomainPrefix": identity.get("hosted_ui_domain_prefix", ""),
    }
    if identity["provider"] == "cognito-saml":
        saml = identity["saml"]
        params.update({
            "SAMLProviderName": saml["provider_name"],
            "SAMLMetadataURL": saml["metadata_url"],
            "SAMLIdpSignout": str(saml["idp_signout"]).lower(),
        })
    return params
