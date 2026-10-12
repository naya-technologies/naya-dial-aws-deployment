# Prerequisites

Collect these before you start. `./dial-install check` verifies every item it can.

## AWS account

- An AWS account and region where you may create: VPC, EKS, IAM roles, RDS/Aurora, ElastiCache,
  S3, ECR, Cognito, CloudFormation stacks and a Route 53 private hosted zone.
  The simplest way is an administrator-level IAM user or role for the duration of the installation.
- An **active AWS Marketplace subscription** to the DIAL product in this account. The platform chart is
  pulled from the Marketplace container registry.
- Service quotas: at least one free VPC and one free Elastic IP in the region (when the installer creates the network).
- **Amazon Bedrock model access** for the models you want to offer (enable them in the Bedrock console).

## Network access of the machine that runs the installer

The installer needs to reach only **AWS service endpoints** (and the registry that hosts the product, which is also an AWS
endpoint). It downloads nothing else: Istio, Knative and the Cluster Autoscaler chart are inside the package.

- **Package `dial-install-<version>-offline-<arch>.tar.gz`** also contains `kubectl`, `helm` and `crane`: use it when the machine
  has no internet access. `./dial-install install-tools` is not needed.
- **Package `dial-install-<version>.tar.gz`** (small) expects those three tools on the machine; `install-tools` downloads them
  from their official sites. AWS CloudShell is a good place for it.

## Container images (default: copied into your own ECR)

By default the installer copies the product images from the AWS Marketplace registry into **your private ECR**
(`registry.provider: ecr-mirror`) and the cluster pulls from there. This needs:

- internet access from the machine that runs the installer (AWS CloudShell has it);
- ECR permissions: `ecr:GetAuthorizationToken`, `ecr:DescribeRepositories`, `ecr:CreateRepository`,
  `ecr:BatchCheckLayerAvailability`, `ecr:InitiateLayerUpload`, `ecr:UploadLayerPart`, `ecr:CompleteLayerUpload`,
  `ecr:PutImage`, `ecr:TagResource`;
- nothing else: images stream registry-to-registry, so no Docker daemon and no local disk space are needed.

About 37 images are copied: the platform workloads, the build tools used by the Deployment Manager, and the cluster add-ons
(Cluster Autoscaler, Istio, Knative), so the cluster needs no access to Docker Hub, Quay, GCR or GHCR for images. Several GB in total. The first copy takes 10-30 minutes; later runs copy only what changed.
Set `registry.provider: public` to skip the copy and pull straight from the Marketplace registry.

## Database sign-in with the IAM role (optional)

With `database.auth: iam` the installer creates the PostgreSQL role for DIAL Admin through a short Job inside the cluster
(the database is private), then installs DIAL so that Admin signs in with its IAM role. Nothing to prepare except:

- the image `naya-dial-postgres:15-alpine` (pinned in the release manifest) must exist in the product registry; it is copied to your ECR like the others. Or set
  `database.bootstrap_image` to any image that contains `psql` and can be pulled by the cluster;
- the role is created with the master password from your secret; afterwards Admin itself uses no password.

## Domain and certificate

- A domain you control, for example `dial.example.com`. DIAL uses these host names (you can rename any of them):
  `chat`, `admin`, `core`, `themes`, `grafana`, `superset` under that domain.
- An **ACM certificate in the same account and region**, status **ISSUED**, that covers every host name.
  A wildcard such as `*.dial.example.com` covers all default names.

## The secret

Create one Secrets Manager secret. `./dial-install init` prepares the file and prints the exact command:

```
aws secretsmanager create-secret --name dial/<name> --secret-string file://dial-secrets.json --region <region>
```

Delete `dial-secrets.json` afterwards and keep the secret's ARN. **Never change its values once DIAL is
installed**; stored data would become unreadable.

## Your own VPC (optional)

Only if you choose `existing-vpc` or `shared-vpc`:

- DNS support and DNS hostnames enabled on the VPC.
- Two private subnets in two different Availability Zones, each with at least 64 free IP addresses.
- Private subnets need a route to the internet (NAT or similar) **or** you let the installer create VPC endpoints
  (`network.create_vpc_endpoints: true`).
- For an internet-facing load balancer: two public subnets with an internet gateway route.
- For a shared VPC: the AWS RAM share must be accepted in this account.

## Tools

`aws` (CLI v2), `kubectl`, `helm` 3.8+ and `crane` (image copy). In CloudShell the AWS CLI is preinstalled. For the
rest run `./dial-install install-tools`, which downloads SHA-256 verified copies into `~/.dial-install/bin`
(`crane` is also downloaded automatically when needed).
