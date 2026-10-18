# Upgrading DIAL

A **release** is one named set of DIAL component versions (see [releases/README.md](../releases/README.md)).
You choose the release; the installer takes the chart, image, add-on, Kubernetes and database versions from it.

## For customers

1. Pick the release: `./dial-install releases` lists those in the package, or use any release id the vendor announces.
2. Set it in `dial.yaml` (`release: <id>`), or pass it once with `--release <id>`.
3. Run:

```
./dial-install check
./dial-install upgrade
```

If the release is newer than your installer package, the installer reads its definition from the platform chart
in AWS Marketplace, so you normally do **not** need to download a new package. You do need one when the
message says the release needs a newer installer.

`upgrade`:

- confirms credentials, the secret and access to the chart;
- shows the installed chart, the target chart, the release notes, and every configuration value that will change
  (secret values are never printed);
- prints the exact **rollback command** for the current revision;
- asks for confirmation, runs one `helm upgrade`, waits for the workloads and verifies the license.

If the release changed the infrastructure templates, `upgrade` stops and tells you to add `--with-infra`:

```
./dial-install upgrade --with-infra
```

If nothing differs, it says **Already up to date** and does nothing.

### Switching the database sign-in

`database.auth` can be changed on an installed environment: set it in `dial.yaml` and run `./dial-install upgrade`. Switching to `iam`
creates the PostgreSQL role first and then moves DIAL Admin to its IAM role; nothing else changes.

### Rolling back

Use the command printed before the upgrade, for example:

```
helm rollback dial 3 --namespace dial --kube-context dial-<name>
```

Helm restores the previous chart and values. Infrastructure changes made with `--with-infra` are not rolled back by Helm.

### Cluster access

Only the IAM principal that ran `apply` can use `kubectl` at first. List other administrators in
`kubernetes.admin_arns` before `apply`, or grant access later (run by someone who already has it):

```
./dial-install grant-access arn:aws:iam::<account>:role/<name>
```

## For the vendor: shipping a new DIAL version

See [releases/README.md](../releases/README.md). In short: build the chart (with `release.yaml` inside), tag it in
Marketplace with the release id (all image versions, add-ons included, are set in that chart), run `./dial-install verify-release`,
optionally add `releases/<id>.yaml` to the installer, bump `infra_revision` if a template changed, `make dist`, and test
`apply` then `upgrade` on a throw-away installation.

## The license helper runs once

`dial-lic-ins` is a one-time helper: it seeds the license records into Redis, reports `OK`, and the installer then deletes its
Deployment. The chart still contains it, so a plain `helm upgrade` would create it again and run it again. `./dial-install apply`
and `upgrade` therefore enable it only for the first installation (or while its Deployment still exists because that installation
did not finish) and pass `lic-ins.enabled=false` otherwise. You do not need to do anything.

