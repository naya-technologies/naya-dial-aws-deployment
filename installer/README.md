# DIAL Installer

Install and upgrade DIAL on AWS from one configuration file.

```
./dial-install init            # answer a few questions -> dial.yaml (and the secrets file)
./dial-install install-tools   # download kubectl, helm and crane into ~/.dial-install/bin
./dial-install check           # verify everything is ready (changes nothing)
./dial-install plan            # show what will be created (changes nothing)
./dial-install mirror          # copy only the product images into your private ECR
./dial-install apply           # build the infrastructure and install DIAL
./dial-install status          # stacks, installed version, DNS records to create
./dial-install upgrade         # move to a newer DIAL release later
./dial-install grant-access <iam-arn>   # give another admin access to the cluster
./dial-install uninstall       # remove everything this installation created (alias: remove)
./dial-install releases        # list the DIAL releases in this package
./dial-install verify-release  # vendor: every image of a release exists in the Marketplace registry
```

## Commands and options

Every command that reads `dial.yaml` accepts the same options:

| Option | Meaning |
|---|---|
| `-c`, `--config <file>` | Configuration file (default `dial.yaml` in the current folder). |
| `--profile <name>` | AWS CLI profile to use (default: the CLI's default credentials; CloudShell needs none). |
| `--release <id>` | Use this DIAL release instead of `release:` in the file (see `releases`). |
| `--chart <dir>` | Vendor: use a local chart directory (it must contain `release.yaml`) instead of the registry, to test a release before publishing it. |

| Command | What it does | Changes AWS? | Extra options |
|---|---|---|---|
| `init` | Asks a few questions, writes `dial.yaml` and `dial-secrets.json`. Never overwrites a config. | No | none |
| `install-tools` | Downloads `kubectl`, `helm` and `crane` (checksum verified). Not needed with the `-offline-` package. | No | none |
| `check` | Verifies tools, credentials, secret, certificate, chart, quotas, images. | No | common |
| `plan` | Lists the stacks and platform changes `apply` will make. | No | common |
| `mirror` | Copies the product images from the Marketplace registry into your private ECR, several at a time, and nothing else. `apply` does the same step and skips what is already there, so run `mirror` first from a fast place (for example CloudShell) to save time. | ECR only | common, `--allow-missing-images N` continue when up to N images cannot be copied |
| `apply` | Creates or updates the infrastructure, copies images, installs DIAL. Safe to repeat; it resumes. | Yes | `-y/--yes` skip the question, `--dry-run` print the commands, `--allow-missing-images N` continue when up to N images cannot be copied (those components stay down) |
| `status` | Shows the stacks, the installed chart version and the DNS records to create. | No | common |
| `upgrade` | Moves an installed DIAL to the release in this package. | Yes | `-y/--yes`, `--dry-run`, `--with-infra` also update the CloudFormation stacks |
| `grant-access <arn>` | Gives an IAM user or role cluster-admin through an EKS access entry. | EKS only | common |
| `uninstall` (alias `remove`) | Removes everything this installation created, in an order that cannot get stuck, and checks at the end that nothing is left. Asks you to type the installation name. Details: [docs/UNINSTALL.md](docs/UNINSTALL.md). | **Deletes** | `-y/--yes`, `--dry-run`, `--keep-images` keep the mirrored images, `--keep-secret`, `--keep-snapshot` keep the final database snapshot |
| `releases` | Lists the releases in this package. | No | none |
| `verify-release` | Vendor: checks that every image of a release exists in the Marketplace registry. | No | common (`--chart <dir>`: check a local chart before publishing) |

Typical order: `init` -> `install-tools` -> `check` -> `plan` -> (`mirror`) -> `apply` -> DNS -> sign in.
To remove an installation use `uninstall`; see [docs/UNINSTALL.md](docs/UNINSTALL.md).

Runs in AWS CloudShell or any Linux/macOS shell with the AWS CLI. Needs only Python 3.9+.
No `pip install`, no repository clone: unpack the package and run it. The `-offline-<arch>` package also contains
`kubectl`, `helm` and `crane`, so it works on a machine without internet access (only AWS endpoints are needed).

## Start here

| You want to... | Read |
|---|---|
| Install DIAL | [docs/INSTALL.md](docs/INSTALL.md) |
| Know what you need first | [docs/PREREQUISITES.md](docs/PREREQUISITES.md) |
| Change a setting | [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| Install a newer DIAL release | [docs/UPGRADE.md](docs/UPGRADE.md) |
| Understand or publish a release | [releases/README.md](releases/README.md) |
| Fix a problem | [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) |
| Remove an installation | [docs/UNINSTALL.md](docs/UNINSTALL.md) |
| Maintain or extend the installer | [docs/DEVELOPING.md](docs/DEVELOPING.md) |

## What it builds

| Layer | Created by | Notes |
|---|---|---|
| Network | `network/create` or `network/existing-vpc` | New VPC, or your own (including RAM-shared) |
| VPC endpoints | `endpoints/create` or `endpoints/none` | For subnets without internet access |
| Storage, cache, database | `storage`, `cache`, `database` | S3 + ECR, ElastiCache Serverless, Aurora PostgreSQL Serverless v2 |
| Cluster | `compute/eks` | EKS, four managed node groups, IAM roles for service accounts |
| Sign-in | `identity/cognito` | Cognito users, or SAML federated through Cognito |
| Cluster prerequisites | Helm chart `dial-cluster-base` (inside this package) | Namespace, storage class, permissions, CRDs |
| Images | `registry/ecr-mirror` | Copies the product images from the Marketplace registry into your private ECR |
| Platform | Helm chart `naya-helm-deployment` from the AWS Marketplace registry | The DIAL umbrella chart; sources in [../charts](../charts/README.md) |

Every layer is a small module. See [docs/DEVELOPING.md](docs/DEVELOPING.md).

## Safety properties

- `check`, `plan` and `status` never change anything. `apply --dry-run` prints every command without running it.
- Secrets live in one AWS Secrets Manager secret that **you** create. The installer only reads it.
- Helm is never run with `--wait`; readiness is verified separately with clear messages.
- Every AWS call carries an explicit region; every kubectl/helm call carries an explicit context.
- Infrastructure state is CloudFormation itself. Nothing is stored on the machine that runs the installer.
- `apply` and `upgrade` never delete anything. Only `uninstall` deletes, only what this installation created
  (stacks carrying the installer's tag, repositories tagged with the installation name), and only after you type the name.

## Status of this version (2.0.0)

Tested: configuration, validation, module contracts, CloudFormation templates (cfn-lint), value generation,
and the complete `check / plan / mirror / apply / upgrade / status` flows against a scripted fake of AWS, Kubernetes
and Helm. Installed end to end on real AWS accounts (new VPC, public load balancer, Cognito sign-in).

Known open items, including the license behavior of `dial-core` 2026.10.1, are listed in
[docs/DEVELOPING.md](docs/DEVELOPING.md#open-items).
