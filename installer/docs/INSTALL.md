# Installing DIAL on AWS - step by step

For: whoever installs DIAL. You do not need to know Kubernetes or CloudFormation.
Time: about 1-2 hours, mostly waiting for AWS. You can stop and run any command again; nothing is repeated twice.

## 0. Before you start (ask your AWS administrator)

Have these ready. If one is missing, the installer will tell you, but it is faster to prepare them first.

| What | Where to get it |
|---|---|
| An AWS account and **region** (for example `us-east-1`) | Your AWS administrator. Use the same region everywhere below. |
| Administrator-level access in that account | An IAM user or role with `AdministratorAccess` while you install. |
| An **active AWS Marketplace subscription** to DIAL in that account | AWS Marketplace -> Manage subscriptions. |
| A **domain** you can edit DNS for, e.g. `dial.example.com` | Your DNS administrator. |
| An **ACM certificate**, status *Issued*, in the **same account and region**, covering `*.dial.example.com` | AWS Certificate Manager -> Request certificate (wildcard) -> validate -> copy its ARN. |
| **Bedrock model access** for the models you want | AWS console -> Amazon Bedrock -> Model access -> enable. |
| The package `dial-install-<version>.tar.gz` | From the vendor. |

Full list with permissions: [PREREQUISITES.md](PREREQUISITES.md).

## 1. Open AWS CloudShell

1. Sign in to the AWS console with the account above and **select the region** (top right).
2. Click the CloudShell icon (`>_`) in the top bar and wait for the prompt.

CloudShell already has the AWS CLI and internet access. If the console tab is closed or the session times out, open
CloudShell again and continue from the step you were on: files in your home folder are kept.

## 2. Upload and unpack the installer

1. In CloudShell choose **Actions -> Upload file** and select the package you were given.
2. Run:

```
tar xzf dial-install-*.tar.gz
cd dial-install-*/
./dial-install install-tools        # skip this if the package name contains "offline"
```

`install-tools` downloads `kubectl`, `helm` and `crane` into your home folder (checksum verified). The **offline** package
already contains them and needs no internet access at all, only access to AWS.

## 3. Create the configuration

```
./dial-install init
```

Answer the questions; press Enter to accept the value in brackets. You will be asked for the region, the domain, the
certificate ARN, whether to create a new network or use an existing one, whether the application is internal or public,
and how users sign in. At the end it writes two files:

- `dial.yaml` - your configuration. Safe to read and edit.
- `dial-secrets.json` - random passwords and keys DIAL needs.

### Create the secret (one command)

`init` prints a command like this. Run it exactly as printed:

```
aws secretsmanager create-secret --name dial/<name> --secret-string file://dial-secrets.json --region <region>
```

It answers with an **ARN** (`arn:aws:secretsmanager:...`). Then:

1. Open `dial.yaml` (`nano dial.yaml`) and paste the ARN after `arn:` under `secrets:` (replace the TODO text).
2. Delete the secrets file: `rm dial-secrets.json`.

Never change the secret's values after DIAL is installed.

## 4. Check everything

```
./dial-install check
```

Each line starts with `[ OK ]`, `[WARN]`, `[TODO]` or `[FAIL]`. For every `[FAIL]` or `[TODO]` the line below it says
what to do. Fix it and run `check` again until you see **All checks passed**. `check` never changes anything in AWS.

## 5. See what will happen

```
./dial-install plan
```

Lists what will be created and the host names that will be used. Nothing is changed.

## 6. Install

```
./dial-install apply
```

Type `y` when asked. The installer then:

1. creates the network, cluster, database, cache and sign-in in AWS (about 30-45 minutes);
2. copies the product images from the AWS Marketplace into your own private ECR (10-30 minutes);
3. prepares the cluster and installs DIAL (10-20 minutes).

Keep the CloudShell tab open. If the connection drops, open CloudShell and run `./dial-install apply` again: it resumes.
To preview every command without running it: `./dial-install apply --dry-run`.

**Faster image copy (optional).** Copying the images is the only step that depends on your network speed. In
CloudShell it takes a few minutes. If you run the installer from a slower machine, open CloudShell (same account and
region), upload the package and `dial.yaml`, and run `./dial-install mirror` there first. `apply` then finds the images
already present and skips the copy. `mirror` copies the images and does nothing else.

Running from your own computer instead of CloudShell: add `--profile <name>` to the commands (or set
`AWS_PROFILE`), and keep `-c <file>` if the configuration has another name.

## 7. Create the DNS records

When it finishes, the installer prints lines like:

```
chat.dial.example.com    -> k8s-dialxxxx-1234567890.us-east-1.elb.amazonaws.com
admin.dial.example.com   -> k8s-dialxxxx-1234567890.us-east-1.elb.amazonaws.com
...
```

In your DNS, create a **CNAME** record for each line (in Route 53 you can use an **Alias to Application Load Balancer**).
You can print them again at any time: `./dial-install status`. DNS can take up to 30 minutes to appear.

## 8. First sign-in

1. Open `https://chat.<your domain>` and `https://admin.<your domain>`.
2. With Cognito sign-in: AWS console -> Amazon Cognito -> your user pool -> create a user. To make that user an
   administrator, create a group named `admin` and add the user to it.
   With SAML sign-in, users sign in through your identity provider; map its administrator group to `admin`.

## If something fails

- Read the message: it says what to fix. Then run the same command again.
- `./dial-install status` shows what exists and which version is installed.
- Common problems and fixes: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
- To ask for help, send the output of `./dial-install check` and `./dial-install status` (they never print secrets).

## Later

- **New DIAL version:** [UPGRADE.md](UPGRADE.md) (`./dial-install upgrade`).
- **More administrators for the cluster:** `./dial-install grant-access <iam-arn>`.
- **Remove an installation:** [UNINSTALL.md](UNINSTALL.md) (`./dial-install uninstall`).
- **Every command and option:** [../README.md](../README.md#commands-and-options).
- **Settings you can change:** [CONFIGURATION.md](CONFIGURATION.md).
