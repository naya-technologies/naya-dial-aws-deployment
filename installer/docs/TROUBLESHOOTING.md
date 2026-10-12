# Troubleshooting

Always start with `./dial-install check`. It reports most problems in plain language.

| Message | Cause and fix |
|---|---|
| `cannot call AWS with the current credentials` | Not signed in. In CloudShell this does not happen. Elsewhere: `aws configure`, or add `--profile <name>`. |
| `config: ... has N problem(s)` | `dial.yaml` has a typo or a missing setting. Each problem is listed with its path. |
| `These settings still contain a TODO placeholder` | Replace the placeholder (usually `secrets.arn`). |
| `Cannot read the secret` | Wrong ARN or region, or your user lacks `secretsmanager:GetSecretValue`. |
| `key <name>: missing` | The secret was not created from the file `init` produced. Re-create it from a complete JSON document. |
| `certificate ... does not cover` | The ACM certificate lacks one of the host names. Request a new one that includes it. |
| `certificate status is PENDING_VALIDATION` | Finish the DNS or email validation in ACM. |
| `cannot pull chart` | The account has no active Marketplace subscription, or lacks `ecr:GetAuthorizationToken`. |
| `stack ... is in state ROLLBACK_COMPLETE` | A previous creation failed. Read the failed events in CloudFormation, delete the stack, run `apply` again. |
| `stack ... failed` during apply | The message includes a command that lists the failed events. Fix the cause, run `apply` again. |
| `cannot get a login for the Marketplace registry` | No active Marketplace subscription in this account, or no `ecr:GetAuthorizationToken`. |
| `aws: [ERROR]: Error when retrieving credentials from container-role ... 500` (CloudShell) | CloudShell itself has no credentials (a console session problem, not your permissions). Restart CloudShell from Actions, open it in a new tab, or sign out and in. If it stays broken, run the installer from another machine with `--profile`. |
| Image copy is slow | It depends on the network of the machine that runs it. Run `./dial-install mirror` from CloudShell (or any machine in AWS) first; `apply` skips images that are already there. |
| Chat shows "Chat is unavailable"; the browser shows `401 Unknown api key` on `/v1/bucket` | `dial-core` could not activate the license. See "Known issues" in [DEVELOPING.md](DEVELOPING.md). When you ask for help, send the output of `./dial-install status` and the logs of `deploy/dial-core`. |
| `Could not copy <image>` | Same causes, or missing ECR push permissions (list in [PREREQUISITES.md](PREREQUISITES.md)). Run `apply` again; images already copied are skipped. |
| Pods stay in `ImagePullBackOff` | Run `./dial-install apply` again (it copies missing images), or check `registry.provider`. |
| `Aurora PostgreSQL <version> is not available` | Set `database.engine_version` to a version listed by the command in the message. |
| `instance type(s) not offered` | Choose other types under `sizing.node_groups`. |
| `Some workloads are not ready yet` | `kubectl --context dial-<name> -n dial get pods`, then `describe` the pod that is not Running. `./dial-install status` re-checks. |
| `License check failed` | Confirm the Marketplace subscription is active for this account, then run `upgrade` again. |
| `kubectl: You must be logged in to the server` | You are not the cluster creator. Ask an administrator to run `./dial-install grant-access <your arn>` (see [UPGRADE.md](UPGRADE.md)). |
| `kubectl` times out | The cluster API is private (`kubernetes.public_endpoint: false`). Run from inside the VPC or a connected network. |
| Browser shows a certificate or DNS error | The DNS records from `status` are missing or have not propagated yet (can take 30 minutes). |

Useful commands:

```
./dial-install status
kubectl --context dial-<name> -n dial get pods
aws cloudformation describe-stacks --stack-name <name>-network --region <region> --query 'Stacks[0].StackStatus'
```

`apply` and `upgrade` are safe to repeat. Nothing is ever deleted by the installer; to remove an installation follow
[UNINSTALL.md](UNINSTALL.md).
