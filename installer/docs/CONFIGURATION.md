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
| `domain.hosts.<service>` | `<service>.<base>` | Full host name override for `chat`, `admin`, `core`, `themes`, `grafana`, `superset`. |
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
| `database.bootstrap_image` | from the chart | Advanced, `auth: iam`: image with `psql` for the one-time role creation. |
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
| `features.container_logs` | `true` | Send container logs and metrics to CloudWatch (Container Insights). |

Edits to this file take effect on the next `apply` (infrastructure) or `upgrade` (platform settings).
Changing `name`, the VPC or the region of an installed environment is not supported; install a new one.

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

