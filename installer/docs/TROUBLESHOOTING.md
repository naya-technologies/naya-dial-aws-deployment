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
| `Could not find the latest version of <add-on>` / `Could not copy ... naya-dial-ai-...` | This machine cannot reach Naya's registry (or, for a private one, your AWS account may not read it: ask Naya), or the version does not exist. Name an existing version with `--version <tag>`. See [ADDONS.md](ADDONS.md). |
| `<add-on> did not become ready` | `kubectl --context dial-<name> -n dial describe pod -l app.kubernetes.io/instance=dial-addon-<id>`. `ImagePullBackOff`: the image is not in your ECR; run the command again. A pod that stays `Pending` for an add-on with its own node group: the node group is not ready or your account has no EC2 quota for the instance type (`--instance-type`). |
| `CloudFormation stack <name>-addon-<id> failed` | The node group could not be created (quota, instance type). Read the failed events with the command in the message; if the stack is `ROLLBACK_COMPLETE`, delete it, then run the command again. |
| `DIAL Core did not become ready after its configuration changed` | An add-on's registration made Core refuse its configuration. The message prints the exact `helm rollback` command that restores the previous one; then `add-ons remove <id>` and report it. |
| `Guardrail <id> version <n> was not found` | Wrong `--guardrail-id`, `--guardrail-version` or `--bedrock-region`, or your user lacks `bedrock:GetGuardrail`. Nothing was installed. |
| `The guardrail version DRAFT is refused` | Publish a numbered version in the Bedrock console, or add `--allow-draft` (testing only). |
| `The Bedrock Guardrail interceptor check failed` | The message says what it means. In short: 404 on `POST /?api-version` = Core calls it without a path (run `add-ons upgrade bedrock-guardrail`); `NoCredentialsError` = its service account has no role or the pod started before it (run `add-ons upgrade`, it restarts it); `AccessDeniedException` = the role may not use this guardrail, or the guardrail is in another account or region; `NoRegionError` = `BEDROCK_REGION` missing; `ResourceNotFoundException` / `ValidationException` = wrong guardrail id or version. Chat says "The safety check is temporarily unavailable": see `kubectl logs deployment/dial-bedrock-guardrail`. Answers arriving all at once is normal while it is attached. |
| `The installation is asleep` | `./dial-install wake`, then run the command again. |
| `The sleep / wake control was not updated` | The worker needs a stable, awake environment to accept a new configuration; the message lists what blocked it (for example a node group still scaling, a pod not ready, Aurora not available). Run the same command again later. |
| A sleep execution fails with `unmanaged-pod` or `unmanaged-node` | Something runs that the installation does not know (added by hand). Remove it, or run `./dial-install apply` so the configuration includes it, then sleep again. |
| A wake execution does not finish | Every public address must answer through the load balancer: create the DNS records (`./dial-install status`). See the execution in Step Functions and the log `/aws/lambda/<name>-sleep-wake-worker`. |
| Browser shows a certificate or DNS error | The DNS records from `status` are missing or have not propagated yet (can take 30 minutes). |

Useful commands:

```
./dial-install status
kubectl --context dial-<name> -n dial get pods
aws cloudformation describe-stacks --stack-name <name>-network --region <region> --query 'Stacks[0].StackStatus'
```

`apply` and `upgrade` are safe to repeat. `apply` and `upgrade` never delete anything; to remove an installation follow
[UNINSTALL.md](UNINSTALL.md).
