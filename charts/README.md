# Helm charts for DIAL (v2)

| Chart | Installed by | Purpose |
|---|---|---|
| `dial-platform` (published as `naya-helm-deployment`) | `dial-install` from the AWS Marketplace registry | Everything DIAL runs: Core, Chat, Admin, Themes, Bedrock adapter, RAG, QuickApps, App Controller, Deployment Manager, analytics (InfluxDB, Grafana, Superset), load balancer controller, license installer |
| `dial-cluster-base` | `dial-install` from inside the installer package | Cluster prerequisites: namespace, gp3 storage class, App Controller permissions, load balancer controller CRDs |

Cluster add-ons that come from upstream (Cluster Autoscaler, Knative, Istio) are installed by the installer at the
versions pinned in the release manifest; they are not repackaged here.

## The placeholder rule

The chart owns **all structure**: images, tags, probes, resources, node selectors, environment variable names.
Only values that differ per installation are written as a token:

```yaml
chat:
  env:
    APP_COGNITO_USER_POOL_ID: 'PLACEHOLDER_COGNITO_USER_POOL_ID'
  ingress:
    hosts: ['PLACEHOLDER_CHAT_PUBLIC_HOST']
```

- A token is `PLACEHOLDER_<NAME>` with `<NAME>` in `A-Z 0-9 _`. It may be the whole value or part of a string
  (`https://PLACEHOLDER_ADMIN_PUBLIC_HOST`).
- Every token is declared in `placeholders.yaml` (source and description). Nothing undeclared is allowed.
- `dial-install` reads the chart's own defaults (`helm show values`), replaces each token, and passes only the
  replaced entries as the values file. Everything else comes from the chart version being installed.
- If a chart uses a token the installer cannot fill, the install stops **before** anything is changed.
- Booleans cannot hold a token. Feature switches that must be booleans (`superset.enabled`,
  `deployment-manager.enabled`) are set by the installer; the same features are string tokens inside env maps.

Every image reference is a **concrete** Marketplace reference (the Marketplace validates the chart with its default values), and the
chart declares in one place which registry that is: `installer.registry`. `dial-install` replaces that registry with
`registry.source` when it differs, or with the customer's own ECR when the images were copied there. To publish from another AWS
account, publish the images and chart there and change `registry.source` (installer or `dial.yaml`); the chart stays as it is.

## Optional value sets

`installer.profiles.<name>.values` holds value sets that `dial-install` merges on request. Today: `rdsIam`, used with
`database.auth: iam` - DIAL Admin signs in to the database with its IAM role (service account annotation, `datasourceAuth: aws`,
CA bundle mount, no password). The image (with `psql`) of the one-time Job that creates the PostgreSQL role is pinned in the
release manifest (`addons.rds_iam_bootstrap_image`), not in the chart, so the chart references only repositories of the listing.

Never put a real secret, an account id, a region or a host name in `values.yaml`.

## Image versions

Every image reference in `values.yaml` is a full Marketplace reference with its tag, and the section `installer.addons`
pins the images of the cluster add-ons (Cluster Autoscaler, Istio, Knative). No template reads that section; `dial-install`
does. Tagging the chart in the Marketplace therefore tags the whole distribution. `./dial-install verify-release` checks that
every referenced repository and tag exists.

## Releasing a chart

1. Change templates/values. Add every new token to `placeholders.yaml` **and** to
   `installer/src/dial_install/values/placeholders.py` (how to compute it).
2. Update `release.yaml` (the release manifest that ships inside the chart) and set `version` in `Chart.yaml`
   to the release id. Copy the same file to `installer/releases/<id>.yaml`.
3. `make test` (runs `helm lint`, renders both charts with the generated values, and fails if a placeholder is
   left, undeclared, unfilled, or if the embedded manifest differs from `installer/releases/<id>.yaml`).
4. `make package`, then `make push REGISTRY=...` and tag the release in AWS Marketplace.

## Changes relative to the previous chart (`platform/base/chart`)

New tokens: `ALB_SCHEME`, `ALB_SUBNETS`, `ALB_GROUP_NAME` (one load balancer for all host names),
`CORE_CONFIG_JSON`, `ANALYTICS_ENABLED`, `DEPLOYMENT_MANAGER_ENABLED`.
Added to the chart (they used to be injected by the old installer): `global.region`, `albcontroller.enabled`,
`aidial.managedSecrets.*`, `rag.updateStrategy`, `dial.bedrock.serviceAccount.name`.
Images are no longer overridden by the installer; every image reference stays in the chart.
