# Developing the installer

Audience: the people who maintain this package. Customers do not need this file.

## Principles

1. **A customer difference is a config value or a module, never a copied file.**
2. "Create it" and "use an existing one" are two providers of the same slot and return the same outputs.
3. CloudFormation is the only state. No local state files.
4. Every command is an argv list run through `runner.py`; writes are marked `write=True` so `--dry-run` and tests can intercept them.
5. Everything the customer reads is English and says what to do next.

## Layout

```
dial-install            launcher (bash -> python3 -m dial_install)
installer.yaml          static defaults: chart repository, node groups, models, tools
releases/               one manifest per DIAL release (all component versions) + index.yaml
schema/dial.schema.json the config contract (validated by src/dial_install/schema.py, stdlib only)
src/dial_install/
  config.py             load, validate, apply defaults, derive host names
  workflow.py           check / plan / apply / upgrade / status
  modules.py            discover + select + order modules
  infra.py              one CloudFormation stack per module, outputs
  bootstrap.py          cluster add-ons (autoscaler, storage class, CRDs, Knative, Istio, private DNS)
  deploy.py             Helm install/upgrade, diff, readiness, license, DNS records
  values/               placeholder substitution: chart defaults + PLACEHOLDER_* -> values file
  charts.py             locate the packaged cluster-base chart
  checks.py secrets.py wizard.py tools.py runner.py cloud.py ...
modules/<slot>/<provider>/   module.yaml, stack.yaml, module.py
assets/                 static files (autoscaler values)
vendor/yaml/            pure-python PyYAML so customers need no pip
tests/                  no AWS access needed
```

## Slots and modules

| Slot | Providers | Requires |
|---|---|---|
| network | create, existing-vpc (also `shared-vpc`) | - |
| endpoints | create, none (derived from `network.create_vpc_endpoints`) | network |
| storage | default | - |
| compute | default (EKS) | network, endpoints |
| links | default | network, compute |
| cache | default | network, compute |
| database | aurora-create | network, compute |
| iam | default | compute, storage, cache, database |
| identity | cognito, cognito-saml | - |
| registry | ecr-mirror (default), public | - |

Order is computed from `requires`; it is not hard-coded.

### Module anatomy

```yaml
# module.yaml
slot: network
providers: [existing-vpc, shared-vpc]
description: Uses a VPC that already exists.
requires: []
provides: [VPCId, PrivateSubnet1Id, ...]     # stack outputs (after finalize) other modules may use
```

```python
# module.py - all hooks are optional
def check(ctx)                     # read-only; return a list of findings (ok/warn/action/fail)
def parameters(ctx)                # CloudFormation parameters, read from ctx.cfg and ctx.outputs[<earlier slot>]
def finalize_outputs(ctx, raw)     # turn raw stack outputs into the slot's output contract
def before_platform(ctx, say)      # runs after the infrastructure exists, before Helm (example: copy images)
def value_overrides(ctx, chart_values)  # Helm values this module contributes (example: point images at your ECR)
```

`stack.yaml` is a plain CloudFormation template (under 51,200 bytes: it is sent inline, no S3 bucket).

### Adding a provider (example: `database/external`)

1. Create `modules/database/external/` with `module.yaml` (`providers: [external]`, same `provides` as `aurora`).
2. No `stack.yaml`; implement `check` (can the cluster reach it?) and `finalize_outputs` (return endpoint, port, name from config).
3. Add the setting to `schema/dial.schema.json` and document it in `docs/CONFIGURATION.md`.
4. Add a contract test next to the existing ones. The shared tests already assert that providers of one slot return the same outputs.

Nothing in `workflow.py` changes. That is the test of a good module boundary.

### Adding or changing a Helm value

The charts live in `../charts` (see its README). The chart owns the structure; the installer only fills
`PLACEHOLDER_<NAME>` tokens. To add a value: put the token in the chart's `values.yaml`, declare it in the chart's
`placeholders.yaml`, and add one line to `src/dial_install/values/placeholders.py`. The contract tests fail if any
of the three disagree. Do not rebuild chart structure in Python.

### Customer-specific needs

Never fork the package. First try a config value. If it is general, add a provider or a parameter.
If it is truly one customer's, it does not belong in the product; keep it in that customer's own repository.

## Testing

```
make test        # unit + contract tests, no AWS access
make lint        # cfn-lint every template
make dist        # tests, then dist/dial-install-<version>.tar.gz + .sha256
```

The contract tests assert, among other things: each template's parameters match what its module passes; every
template reference is defined; no template output is a secret; both network providers return identical outputs;
the database password never appears on a command line; Helm is never run with `--wait`; every AWS call has a region
and every kubectl/helm call has a context.

## Security posture

Done: every IRSA role trusts exactly one service account (audience and subject conditions); the Core role reaches
only the DIAL bucket; client secrets never appear in stack outputs; the database password never appears on a
command line; chart files contain no concrete secrets; the secret is read-only to the installer.

Hardening backlog (needs a validated install first, because narrowing a policy can break a feature):

- `BedrockServiceRole` uses `AmazonBedrockFullAccess`; narrow it to invoke/converse/list actions.
- `AppControllerServiceRole` uses `AmazonEC2ContainerRegistryFullAccess`; narrow it to the `aidial/*` repositories.
- Secrets Manager and License Manager statements on `Resource: '*'` in the Core role where the API allows narrowing.
- `EKS` API endpoint is public by default (IAM protected); `kubernetes.public_endpoint: false` makes it private.

## Known issues and limitations

- **Chat sign-in with `dial-core` 2026.10.1 (server 0.45.7).** `GET /v1/bucket` returns `401 Unknown api key` for a valid
  token, while `/openai/models` and `/v1/user/info` return 200. `Proxy.authorizeRequest` turns any exception from
  `LicenseChecker.onboardUser` into that message. What is known: Core needs the Redis keys `licKey`, `apilicKey` and
  `activeTiersKey` (the installer creates the last two when the license installer does not, see
  `deploy.ensure_api_key_license`); `LicenseChecker.findUsersMax` parses `Entitlement.value()` as JSON, which is `null` for
  Marketplace contract entitlements; and `increaseTierUsage` reads the checkout token by serializing an AWS SDK v2 response
  with Jackson, so the token is lost after a successful checkout and the consumed unit is never recorded. A `PERPETUAL`
  checkout cannot be checked in, so a unit consumed this way cannot be returned through the API.
- `database.auth: iam` needs a `naya-dial-postgres` repository (tag `15-alpine`) in the product registry for the one-time Job.
  Until it is published, set `database.bootstrap_image`, or leave `database.auth` at `password`.
- The IAM database login covers DIAL Admin only. The Deployment Manager and Superset keep the password.
- Changing the instance type of an existing node group fails in CloudFormation (the node group has a fixed name, so the
  replacement collides). Create a new group or reinstall.
- Cluster Autoscaler manages the `dynamic` node group only, by design; the other groups keep `min = max`.
- `dial-lic-ins` prints the API key of the Admin project in the Core logs ("Trying to onboard api-key").
- Cilium images are not part of the Marketplace listing; the installer copies them from `quay.io/cilium`.
- Several allowed source networks (`network.allowed_cidr`) are limited to five.

Verified end to end on AWS: the chart and the images are pulled from the Marketplace registry, Aurora `15.13`, the
`{{resolve:secretsmanager:...}}` password reference, Knative and Istio of the release, the Cognito group `admin` mapping
to the role `admin`, both network plugins (VPC CNI and Cilium), and a clean installation in about 50 minutes.

Image tags are verified against the registry inventory in `tests/fixtures/registry-inventory.json`
(`tests/test_mirror.py::RegistryInventoryTests` checks every image the chart uses). Refresh the fixture when a release
adds or renames images.

Roadmap (the module boundaries already allow these without changing the core):

- `database/external`, `cache/external`, `identity/oidc`.
- Adopting an existing environment in place.
