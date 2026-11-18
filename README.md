# DIAL on AWS

Install [DIAL](https://dialx.ai) on your own AWS account from the AWS Marketplace listing, with one configuration
file and a handful of commands. You run it yourself from **AWS CloudShell**; nothing needs to be installed on your
computer.

## What you get

- A private EKS cluster, Aurora PostgreSQL database, ElastiCache Redis, Cognito sign-in (or your SAML provider),
  load balancer with your certificate, and the DIAL platform on top.
- The product images copied into **your own** private ECR registry.
- Optional **add-ons** (AI Brain, AI RAG, ...) that you install, upgrade and remove one by one with `./dial-install add-ons`.
- Later upgrades with one command, and a clean removal with one command.

Typical time: 1-2 hours, mostly waiting for AWS.

## Quick start

1. Make sure you have what [docs/PREREQUISITES.md](installer/docs/PREREQUISITES.md) lists: an AWS account with an active
   Marketplace subscription, a domain, and an issued ACM certificate.
2. Open AWS CloudShell, upload the package `dial-install-<version>.tar.gz`, then:

```
tar xzf dial-install-*.tar.gz && cd dial-install-*/
./dial-install install-tools
./dial-install init        # answer the questions
./dial-install check       # fix every [FAIL] / [TODO], run again until it passes
./dial-install apply       # creates everything and installs DIAL
```

3. Create the DNS records the installer prints, then open `https://chat.<your domain>`.

Every step in detail: **[installer/docs/INSTALL.md](installer/docs/INSTALL.md)**.

## Optional add-ons

Beyond the base platform there are optional products from Naya's own registry. They are part of the same tool:

```
./dial-install add-ons list                 # what exists, and what is installed in your environment
./dial-install add-ons install ai-brain     # copies the images into your ECR, installs, registers in DIAL Core
./dial-install add-ons upgrade ai-brain     # the newest version
./dial-install add-ons remove ai-brain      # removes everything the add-on created
```

Each add-on gets its own Helm release, its own images in your ECR and, where it needs one (AI RAG), its own node group, and never changes the
platform's version. Details, settings and flags: **[installer/docs/ADDONS.md](installer/docs/ADDONS.md)**.

## Documentation

| I want to... | Read |
|---|---|
| Install DIAL, step by step | [installer/docs/INSTALL.md](installer/docs/INSTALL.md) |
| Know what to prepare first | [installer/docs/PREREQUISITES.md](installer/docs/PREREQUISITES.md) |
| Change a setting (network, sizes, sign-in, Cilium...) | [installer/docs/CONFIGURATION.md](installer/docs/CONFIGURATION.md) |
| Install an optional add-on (AI Brain, ...) | [installer/docs/ADDONS.md](installer/docs/ADDONS.md) |
| Move to a newer DIAL version | [installer/docs/UPGRADE.md](installer/docs/UPGRADE.md) |
| Fix a problem | [installer/docs/TROUBLESHOOTING.md](installer/docs/TROUBLESHOOTING.md) |
| Remove everything | [installer/docs/UNINSTALL.md](installer/docs/UNINSTALL.md) |
| See every command and option | [installer/README.md](installer/README.md) |
| Work on the installer itself | [installer/docs/DEVELOPING.md](installer/docs/DEVELOPING.md) |

## Repository layout

```
installer/   the dial-install tool: CLI, CloudFormation modules, tests, documentation
charts/      Helm charts installed by the tool (dial-platform, dial-cluster-base)
```

Build the customer package with `make -C installer dist`; run the tests with `make -C installer test`.
