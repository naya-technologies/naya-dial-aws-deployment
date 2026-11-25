# Configuration reference (`dial.yaml`, apiVersion `dial/v1`)

`./dial-install init` writes this file. Only the settings below ever need to be edited. Anything not listed
has a safe default. The full machine-readable definition is [schema/dial.schema.json](../schema/dial.schema.json);
working examples are in [examples/](../examples/).

| Setting | Default | Meaning |
|---|---|---|
| `release` | `latest` | The DIAL release to install. Every component version comes from it (see [releases/README.md](../releases/README.md)). `./dial-install releases` lists the ones in this package; any release published in Marketplace also works. |
| `name` | required | Short name (2-21 lowercase letters, digits, dashes). Prefix of every stack and the cluster. |
| `aws.region` | required | Region to install into. |
| `aws.account_id` | none | Safety net: `check` fails if your credentials belong to another account. |
| `domain.base` | none | Base domain. Hosts become `<service>.<base>`. Required unless every host is listed. |
| `domain.hosts.<service>` | `<service>.<base>` | Full host name override for `chat`, `admin`, `core`, `themes`, `grafana`, `superset`, `flows` (Flow Builder editor), `flows-core` (Flow Builder backend; only `/api` and `/auth` are reachable from outside). |
| `domain.certificate_arn` | required | ISSUED ACM certificate covering every final host name. |
| `secrets.arn` | required | ARN of the Secrets Manager secret you created. |
| `network.provider` | `create` | `create`, `existing-vpc` or `shared-vpc` (RAM). |
| `network.cidr`, `network.subnet_cidrs` | `10.0.0.0/16` | For `create`. Give the four subnet CIDRs when the VPC is not a /16. |
| `network.vpc_id`, `network.subnets.private/public` | none | For existing/shared VPCs. Two subnets in two zones each. |
| `network.exposure` | `internal` | Load balancer scheme: `internal` or `public`. |
| `network.allowed_cidrs` | `[0.0.0.0/0]` | Up to 5 source networks allowed to reach the load balancer (HTTPS, and HTTP for the redirect). |
| `network.create_vpc_endpoints` | `true` for `create`, else `false` | Create the VPC endpoints for AWS services. |
| `identity.provider` | `cognito` | `cognito` or `cognito-saml`. |
| `identity.saml.*` | none | `provider_name`, `metadata_url` (https), `idp_signout`. Required for SAML. |
| `identity.self_registration` | `false` | Let users sign themselves up. |
| `database.min_capacity`, `max_capacity` | `1`, `4` | Aurora Serverless v2 capacity (ACU). The database scales between the two on its own; raise `max_capacity` if it becomes the bottleneck. |
| `database.auth` | `password` | How DIAL Admin signs in to the database: `password` or `iam` (its IAM role: no password, short-lived tokens). The Deployment Manager and Superset still use the password. |
| `database.iam_user` | `dial_admin_iam` | PostgreSQL role used with `auth: iam`. |
| `database.core_bootstrap` | from the chart | Override the chart bootstrap method. `jdbc` uses a Core image that includes `LicensingDatabaseBootstrap` and runs Flyway before Helm. |
| `database.bootstrap_image` | from the chart | Image with `psql` when the installer uses the legacy bootstrap. |
| `database.backup_retention_days` | `7` | Days automated backups are kept (1-35). |
| `database.deletion_protection` | `true` | The database cannot be deleted while enabled. |
| `database.engine_version` | from the release | Aurora PostgreSQL version; `check` confirms AWS still offers it. |
| `kubernetes.version` | from the release | EKS version. |
| `kubernetes.cni` | `vpc-cni` | Network plugin. `cilium` uses an overlay network (see below). Choose it before the first installation; it cannot be changed later. |
| `kubernetes.pod_cidr` | `100.96.0.0/18` | Only with `cilium`: the pod range (one /24 per node). It must not overlap the VPC or any network you connect to. |
| `kubernetes.public_endpoint` | `true` | `false` makes the cluster API private; run the installer from inside the VPC. |
| `kubernetes.admin_arns` | `[]` | IAM users/roles that get cluster-admin in addition to whoever installs. Later: `./dial-install grant-access <arn>`. |
| `tags` | `{}` | Tags added to every AWS resource the installer creates (cost center, owner, ...). |
| `sizing.node_groups.<group>` | from `installer.yaml` | Override `instance_type`, `min`, `desired`, `max` (and `disk_gb` for `core`). Groups: `core`, `dynamic`, `system`. |
| `registry.source` | from `installer.yaml` | `oci://<registry host>/<repository prefix>`: where the platform chart and every product image are published. Change it to use another AWS account; nothing else names an account. |
| `registry.provider` | `ecr-mirror` | `ecr-mirror` copies the product images into your private ECR at install and upgrade and the cluster pulls from there; `public` pulls straight from the Marketplace registry. |
| `features.analytics` | `true` | Superset, Grafana, InfluxDB. |
| `features.deployment_manager` | `true` | The application deployment manager. |
| `features.desktop_app` | `false` | Support the DIAL Desktop application: `apply` creates its Cognito app client (public, no secret, authorization code flow, callbacks `nayadial://auth/callback` and `nayadial://auth/logout`, refresh token 30 days) and the chat is told its id (`/config.js` then contains `desktop.cognitoClientId`). Turning it on for an installed environment needs `./dial-install upgrade --with-infra`. See below. |
| `power.schedule` | none | Sleep and wake automatically: `sleep`, `wake` (HH:MM), `days` (default `SUN-THU`), `timezone` (default `Asia/Jerusalem`). Without it the state machines `<name>-sleep` and `<name>-wake` exist and are started by hand. `power.enabled: false`: no sleep / wake control. See [SLEEP_WAKE.md](SLEEP_WAKE.md). |
| `features.container_logs` | `true` | Send container logs and metrics to CloudWatch (Container Insights). |

Edits to this file take effect on the next `apply` (infrastructure) or `upgrade` (platform settings).
Changing `name`, the VPC or the region of an installed environment is not supported; install a new one.

Optional products are not configured in this file: see [ADDONS.md](ADDONS.md) (`./dial-install add-ons`).

## Models

DIAL registers one model, with the stable id **`default.model`** and the display name **System Default Model**, served by an adapter that DIAL Admin shows as **AWS Private Bedrock**. Every
component refers to that id; which Bedrock model answers is decided only by its endpoint, so you can change the model
in one place without touching anything else.

The model is defined in `installer.yaml` under `models:`

| Key | Meaning |
|---|---|
| `key` | The model id inside DIAL. Keep `default.model`. |
| `display_name` | Name shown to users. |
| `bedrock_model_id` | The Bedrock model, without the inference-profile prefix (for example `anthropic.claude-sonnet-5-5`). |
| `topics` | Optional topics (DIAL Core calls them `descriptionKeywords`). `hidden` keeps the model out of the model list and the catalog of the chat UI; it still works when named directly. The default model is hidden. |
| `features` | Optional DIAL Core model features, for example `temperatureSupported: false` (the default model needs it: Claude Sonnet 5 rejects the `temperature` parameter, and the chat UI then does not send it). |
| `inference_profile` | `global`, `us`, `eu`, `apac`, `auto` (from the region) or `none` (use `bedrock_model_id` exactly as written). Default `auto`. |

The endpoint DIAL calls is `.../openai/deployments/<inference_profile>.<bedrock_model_id>/chat/completions`.
With the shipped defaults that is `global.anthropic.claude-sonnet-5-5`. Enable access to that model in the Amazon Bedrock
console of your account and region; the adapter's IAM role already allows Bedrock.

The adapter name is the `adapters:` section of `installer.yaml` (`bedrock.display_name`). DIAL Core has no such setting: the
installer sets it in DIAL Admin after the workloads are ready (a warning, never an error, if that step fails).

To use another model, change `bedrock_model_id` (and `inference_profile` if needed) and run `./dial-install upgrade`.

## Application types (Agent Builder)

DIAL Core offers application types (application runners) that people use to create applications. The installer registers them in
`installer.yaml` under `application_types:`. Today there is one, **Agent Builder** (QuickApps, id
`https://mydial.epam.com/custom_application_schemas/quickapps2`); its display name, endpoints and options are generated from the
entry (`display_name`, `component`, `deployment`, `configuration_endpoint`, `append_properties_header`, `assistant_attachments`).
Change `display_name` and run `./dial-install upgrade` to rename it. Keep the `id`: existing applications refer to it.

## Cilium (overlay network)

```
kubernetes:
  cni: cilium
```

What changes: pods get addresses from `kubernetes.pod_cidr` instead of the VPC, so the VPC cannot route to them.
The installer therefore creates the cluster in two steps (control plane, then Cilium, then the nodes), points the load
balancers at the nodes (NodePort services, `target-type: instance`), runs the webhooks that the control plane calls with
host networking, keeps two system nodes, and leaves Container Insights off.

Cilium's images are not in the Marketplace listing, by decision: whoever chooses Cilium provides access to its
registry. The installer copies them from `quay.io/cilium` into your ECR (`naya-dial-cilium` and
`naya-dial-cilium-operator`), so the machine that runs it needs internet access to `quay.io`; `check` verifies it.
To use your own mirror, change the sources under `cilium.images` in `installer.yaml`. The Cilium chart itself is
inside the installer package.


## DIAL Desktop application

```
features:
  desktop_app: true
```

The desktop application signs in through Cognito with its own app client, `dial-x-desktop`: a public client (no secret), authorization
code only, scopes `openid email profile`, the same identity providers as the chat client (so SAML works too), and the custom-scheme
callbacks above. The installer creates it with the rest of the sign-in stack and gives its id to the chat (`APP_COGNITO_DESKTOP_CLIENT_ID`).
Nothing else changes: no secret, no host name, no DNS record.

DIAL Core does not restrict the audience of the tokens it accepts in this installation (its Cognito provider has no `audiences` setting), so the
new client needs no change there. The Cognito Hosted UI created by the installer is the classic one, so no managed-login style is needed.

### The connection file

At the end of `apply` and `upgrade`, and again whenever you run `./dial-install status`, the installer writes **`<name>.dialx.json`** next to `dial.yaml`, replacing
the old one, and prints its path and content. Your users import this one file in DIAL X Desktop (New connection, Import file, Save, then click the
connection to sign in); they type no address or id. It holds the addresses of core, chat, admin and flows, the Cognito region, user pool, domain and
Desktop client id, and the id of the managed chat application. It contains no secret, but it lists where your environment is: share it with your users, do not publish it.

Before it is handed over the installer checks it, and stops with the name of the check that failed (and deletes the file): `file-format` (valid JSON,
under 64 KB, all values text), `urls-https`, `no-secrets`, `servers-answer`, `chat-config-has-client` (the chat's `/config.js` contains the Desktop
client id, which proves the chat picked up the setting) and `cognito-authorize` (Cognito's sign-in page accepts the client). Checks that need
your DNS records show `[WAIT]` while the records do not exist yet; create them and run `./dial-install status` to check again.
