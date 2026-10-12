# DIAL on AWS

Install [DIAL](https://dialx.ai) on your own AWS account from the AWS Marketplace listing, with one configuration
file and a handful of commands. You run it yourself from **AWS CloudShell**; nothing needs to be installed on your
computer.

## What you get

- A private EKS cluster, Aurora PostgreSQL database, ElastiCache Redis, Cognito sign-in (or your SAML provider),
  load balancer with your certificate, and the DIAL platform on top.
- The product images copied into **your own** private ECR registry.
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

## Documentation

| I want to... | Read |
|---|---|
| Install DIAL, step by step | [installer/docs/INSTALL.md](installer/docs/INSTALL.md) |
| Know what to prepare first | [installer/docs/PREREQUISITES.md](installer/docs/PREREQUISITES.md) |
| Change a setting (network, sizes, sign-in, Cilium...) | [installer/docs/CONFIGURATION.md](installer/docs/CONFIGURATION.md) |
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
