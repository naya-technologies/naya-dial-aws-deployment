# Changelog - naya-helm-deployment (dial-platform)

## 2026.10.4

- **Fixes "Chat is unavailable. Please contact your administrator."** The chat UI runs every conversation with a model through the
  application `managed-model-chat` (served by QuickApps). The chart did not register it, and the chat UI was not told its id. Now
  DIAL Core loads it from its own ConfigMap-backed file (`/mnt/system-config/managed-model-chat.json`, listed in
  `aidial.config.files`), so configuration exported by DIAL Admin cannot drop it, and `APP_MANAGED_CHAT_APPLICATION_ID` of chat names it.
- The only model name left in the chart, `PY_INTERPRETER_ADDITIONAL_HANDLING_MODEL` of QuickApps, now names `default.model`
  (it named a model that DIAL Core does not register).
- The installer registers the default model with `temperatureSupported: false`: Claude Sonnet 5 on Bedrock rejects the `temperature`
  parameter (HTTP 400), and the chat UI sends it unless the model says it is not supported.
- **DIAL Admin no longer shows models as OUT OF SYNC.** `CORE_CONFIG_VERSION` of admin-backend was `0.39.2` while dial-core
  is `0.45.7`; Admin read Core's state in the old schema, so it never matched what Admin exported. It is now `0.45.7`.
- The installer registers the default model with the topic `hidden` (Core: `descriptionKeywords`), so the chat UI and the catalog do not
  list it.
- **Flow Builder is part of the platform:** `flows-core` (the backend that runs flows for DIAL Core and serves the editor's API) and
  `flows` (the visual editor), from `naya-dial-flows-backend` and `naya-dial-flows-frontend` `2026.10.1`. The ALB exposes the backend
  only on `/api` and `/auth`; DIAL Core reaches the runner and the chat server reaches the published-flow catalog inside the cluster.
  Core registers the application type `Flow Builder` (`https://FlowBuilder`); QuickApps is shown as `Agent Builder`.
- **Infrastructure change (`infra_revision` 3):** two new host names (`flows`, `flows-core`) and the Cognito sign-in callbacks for
  them. The secret needs the key `flows_session_secret`. An upgrade needs `--with-infra`.
- Images unchanged from 2026.10.3, plus the two Flows images.

## 2026.10.3

- **RAG is no longer part of the platform chart.** The `rag` sub-chart, its values and its image (`naya-dial-rag`) are removed. RAG is
  an optional module installed separately from its own registry. The platform no longer contains anything that names an LLM
  deployment directly.
- **The dedicated RAG node group is removed** (`infra_revision` 2). Upgrading an existing environment needs `--with-infra`.
- Updates from 2026.10.2 stay: `naya-dial-core` and `naya-dial-lic-ins` at `2026.10.2`; every other image at `2026.10.1`.

## 2026.10.2

Updates two components. Every other image stays at `2026.10.1`; the chart structure and the placeholders are unchanged.

- `naya-dial-core`: `2026.10.1` -> `2026.10.2`
- `naya-dial-lic-ins`: `2026.10.1` -> `2026.10.2`

Upgrade with `./dial-install upgrade` (no `--with-infra` needed: `infra_revision` is unchanged).

## 2026.10.1

**Published** 2026-10-08 to `709825985650.dkr.ecr.us-east-1.amazonaws.com/naya-technologies-by-epam/naya-helm-deployment:2026.10.1`
(digest `sha256:77e2709ef274e80890341911b4b64047df194427541072f472d8c5921e70648f`). The package pulled back from the registry is
byte-identical to the one pushed, and renders (97 resources).

**Images verified in the registry:** all 43 references the release needs (workloads, build tools, cluster add-ons) exist under that exact
name and tag. Every product image carries the release tag `2026.10.1`; the variants are `naya-dial-python:2026.10.1-3.8 ... -3.12`,
`naya-dial-mcp-proxy:2026.10.1-alpine|-debian` and `naya-dial-buildkit:2026.10.1-rootless`. Only the optional
`naya-dial-postgres` image (database.auth: iam) is not published yet.

The first chart built for `dial-install` 2.x. It contains placeholders that only `dial-install` fills, so install it with
`dial-install` and not with `helm install` directly.

### Relocatable
- The chart declares the registry its images come from once (`installer.registry`); every image reference is concrete, which is what
  the Marketplace validates. `dial-install` replaces that registry with `registry.source`, or with the customer's own ECR when images
  are copied, so publishing from a different account needs no chart change.

### Database sign-in with the IAM role (new, optional)
- `installer.profiles.rdsIam`: the value set for `database.auth: iam` (DIAL Admin uses its IAM role; no password in its resources).
  Default remains the password. The one-time role creation uses the image `naya-dial-postgres:15-alpine`, pinned in the release manifest (not in the chart).

### Region independence
- The RDS CA bundle used by the optional IAM database login (`adminRdsIam`, off by default) is now AWS's **global** bundle
  (`files/aws-rds-global-bundle.pem`, valid for every region) instead of the us-east-1 one. Nothing else in the chart names a region;
  the only `us-east-1` left is the host of the registry that publishes the images.

### Complete image pinning
- Repository names that did not exist in the Marketplace registry were corrected: `naya-dial-superset`,
  `naya-dial-deployment-manager`, `naya-dial-file-processing`.
- Build tools of the Deployment Manager (buildkit, buildkit-rootless, syft, skopeo, git, mcp-proxy) and the app builder image
  are now registry images instead of docker.io / quay.io / ghcr.io references.
- New section `installer.addons`: image of the Cluster Autoscaler, Istio (pilot, proxy) and Knative (7 images). Read by
  `dial-install`, not by any template.
- Embedded `release.yaml` (release manifest) and `placeholders.yaml` (placeholder contract) at the chart root.

### Placeholders added
`ADMIN_DB_IAM_USER`, `ADMIN_BACKEND_RDS_ROLE_ARN`, `ALB_SCHEME`, `ALB_SUBNETS`, `ALB_GROUP_NAME` (one load balancer for all hosts),
`CORE_CONFIG_JSON`, `ANALYTICS_ENABLED`, `DEPLOYMENT_MANAGER_ENABLED`.

### Settings moved into the chart (the old installer injected them)
`global.region`, `albcontroller.enabled`, `aidial.managedSecrets.*`, `rag.updateStrategy`, `dial.bedrock.serviceAccount.name`.

### Not changed
Subchart versions (dial 5.17.0, dial-admin 0.17.0, aws-load-balancer-controller 1.14.1, dial-extension 2.1.0,
grafana 10.5.12, influxdb2 2.1.2) and all component image tags (2026.4.1 and friends).

## dial-cluster-base 1.0.0
Namespace, gp3 storage class, App Controller permissions and the load balancer controller CRDs. Ships inside the installer.
