# Releases

A **release** is one named, complete set of DIAL component versions. You manage the release;
the installer takes every version from it. The customer only chooses the release.

```yaml
# dial.yaml
release: 2026.10.1     # or `latest`
```

## What a release pins

**Every image version lives in the platform chart that you tag in the Marketplace.** The chart tag is the release id.
That includes the add-ons the installer sets up next to the platform: `values.yaml` has a section the installer reads
(and no template uses):

```yaml
installer:
  addons:
    clusterAutoscaler: 709825985650.dkr.ecr.us-east-1.amazonaws.com/naya-technologies-by-epam/naya-dial-cluster-autoscaler:2026.10.1
    istio:
      pilot: .../naya-dial-istio-pilot:2026.10.1
      proxy: .../naya-dial-istio-proxyv2:2026.10.1
    knative:
      serving-activator: .../naya-dial-knative-activator:2026.10.1
      ...
```

So the installer never carries its own list of image tags: it renders the chart of the chosen release, finds every image
(workloads, build tools named in the values, add-ons), checks that each exists, copies it into the customer's ECR and uses the copy.

`releases/<id>.yaml` (format `dial-release/v1`) holds only what is not an image:

| Key | Meaning |
|---|---|
| `release` | The release id. For releases published in Marketplace this is the chart tag. |
| `chart.version` | Platform umbrella chart version (equal to the release id). |
| `kubernetes.default_version`, `database.engine_version` | Infrastructure versions tied to this release. |
| `addons.*` | Upstream versions of the add-on software the installer runs or downloads (autoscaler Helm chart, `istioctl`, Knative manifests). |
| `infra_revision` | Bump when any CloudFormation template under `modules/` changes. `upgrade` then requires `--with-infra`. |
| `min_installer_version` | Older installers refuse the release and ask for a newer package. |
| `notes` | Shown to the customer before an upgrade. |

Static settings (node groups, models, `registry.source`) live in `installer.yaml`.

## Where the installer finds a release

1. `releases/<id>.yaml` inside the package (works offline). `releases/index.yaml` lists them and names `latest`.
2. Otherwise, the file `release.yaml` at the root of the platform chart tagged `<id>` in the Marketplace registry.

So a release you publish in Marketplace after the installer was built can be installed or upgraded to
**without a new installer package**, as long as it needs no new installer code
(`min_installer_version`) and its `infra_revision` is handled with `--with-infra`.

## Publishing a release (vendor)

Only the platform chart goes to the Marketplace repository `naya-technologies-by-epam/naya-helm-deployment`.
`dial-cluster-base` ships inside the installer package. Commands, from the repository root:

```
# 1. Edit charts/dial-platform: image tags in values.yaml (installer.addons included), `version` in Chart.yaml
#    (= the release id), and release.yaml. Copy release.yaml to installer/releases/<id>.yaml and update installer/releases/index.yaml.
make -C v2 test

# 2. Check that every image the chart references exists (before publishing anything)
cd installer && ./dial-install verify-release --chart ../charts/dial-platform -c dial.yaml

# 3. Package and push
cd ../charts && make package
aws ecr get-login-password --region us-east-1 | helm registry login --username AWS --password-stdin 709825985650.dkr.ecr.us-east-1.amazonaws.com
make push REGISTRY=oci://709825985650.dkr.ecr.us-east-1.amazonaws.com/naya-technologies-by-epam

# 4. In AWS Marketplace add the new version to the listing (the chart tag is the release id; new image
#    repositories such as the Istio and Knative ones must be part of the version's delivery option)
# 5. Build and publish the installer:   make -C v2 dist
```

`helm push` stores the chart as `.../naya-technologies-by-epam/naya-helm-deployment:<version>`. This was checked locally against a
test registry: after the push, `helm pull --untar` returns `release.yaml` and `placeholders.yaml`, and `helm show values` returns the
`installer:` section.

### Steps in short

1. Build and test the platform chart. Set the tag of every image in `values.yaml`, including `installer.addons`. Add `release.yaml` to the chart root (a copy of the manifest;
   make sure `.helmignore` does not exclude it).
2. Push the chart and tag it in Marketplace with the release id (`helm push` to the Marketplace repository).
3. Optional, recommended: also add `releases/<id>.yaml` to the installer, update `releases/index.yaml`
   (`latest`), run `make test`, and `make dist`. This makes the release available offline and selectable
   in `init`.
4. If a template under `modules/` changed, bump `infra_revision` in the manifest.
5. Run `./dial-install verify-release --release <id>` with an account that can read the Marketplace registry: it lists every
   image the release needs (chart workloads, build tools, add-ons) and fails if any repository or tag does not exist.
6. Verify on a throw-away installation: `apply` with the previous release, then `upgrade` to the new one.
