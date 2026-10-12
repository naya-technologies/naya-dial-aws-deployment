# Removing an installation

```
./dial-install uninstall              # shows what will be removed, asks you to type the installation name
./dial-install uninstall --dry-run    # prints the commands, changes nothing
./dial-install uninstall -y           # no question (scripts)
```

Options: `--keep-images` keeps the mirrored product images in your ECR (the next installation is faster),
`--keep-secret` keeps the Secrets Manager secret, `--keep-snapshot` keeps the final database snapshot.
**The database and everything stored in it is deleted.** The command is safe to run again; it continues.

What it does, in this order:

1. Reports the AWS Marketplace license usage (see "License" below).
2. Removes the ingresses and the Istio gateway while the load balancer controller still runs, waits until the
   load balancers are gone, then uninstalls the Helm releases and deletes the volumes and namespaces.
3. Removes the private DNS zone `<namespace>.knative.<name>.internal`.
4. Turns off the database deletion protection.
5. Deletes the independent stacks together, then removes load balancers, target groups and security groups left
   behind, then `endpoints`, `network` and `storage` (emptying the bucket and the repositories first).
6. Deletes the mirrored images, the secret, the log groups and the final database snapshot (unless kept).
7. Checks that nothing of the installation is left and lists what is, exiting with an error if something is.

It touches only what the installation owns: stacks that carry the installer's tag, repositories tagged
`dial-install:installation=<name>`, and objects named after the installation.

## License

Core activates a license tier with a `PERPETUAL` checkout. AWS License Manager refuses `CheckInLicense` for that
type ("cannot be checked in"), so **a consumed unit cannot be returned through the API**. `uninstall` shows the
consumed units; to get them back, contact AWS Marketplace seller support. Note that deleting Redis deletes the
record of the activation, so a reinstall in the same account activates (and consumes) a unit again.

## Doing it by hand

The same procedure, if you cannot run the command. Replace `<name>` with the `name:` from `dial.yaml`.

### 1. Remove the load balancers and the application

Do this while the cluster still runs. The AWS Load Balancer Controller removes the load balancers it created only while it
exists; if you delete the application first, load balancers stay behind and block the network.

```
kubectl --context dial-<name> -n dial delete ingress --all
kubectl --context dial-<name> -n istio-system delete svc istio-ingressgateway
# wait until no load balancer named k8s-* is left:
aws elbv2 describe-load-balancers --query 'LoadBalancers[].LoadBalancerName' --region <region>

helm --kube-context dial-<name> uninstall dial -n dial
helm --kube-context dial-<name> uninstall cluster-autoscaler -n kube-system
helm --kube-context dial-<name> uninstall dial-cluster-base -n kube-system
kubectl --context dial-<name> -n dial delete pvc --all
```

### 2. Remove the private DNS zone

`dial.knative.<name>.internal` (Route 53, private). Delete its records, then the zone.

### 3. Turn off database deletion protection

```
aws rds modify-db-cluster --db-cluster-identifier <name>-db --no-deletion-protection --apply-immediately --region <region>
```

### 4. Delete the stacks

The stacks do not depend on each other in CloudFormation, so these can be deleted at the same time:
`<name>-identity`, `<name>-iam`, `<name>-database`, `<name>-cache`, `<name>-links`, `<name>-compute`.
When `compute` is gone, delete `<name>-endpoints`, then `<name>-network`, then `<name>-storage`.

```
aws cloudformation delete-stack --stack-name <name>-compute --region <region>
aws cloudformation wait stack-delete-complete --stack-name <name>-compute --region <region>
```

If `network` fails to delete, a security group or network interface of a load balancer is left: delete the
`k8s-*` security groups of that VPC, then delete the stack again.

### 5. Things the stacks leave behind

| What | Why | Remove with |
|---|---|---|
| Product images in ECR (`naya-technologies-by-epam/*`) | Created by `apply`, not by a stack. They cost storage. Keep them to make the next installation faster. | `aws ecr delete-repository --repository-name <repo> --force` |
| The storage bucket | `DeletionPolicy: Retain` (customer data). | Empty it, then `aws s3api delete-bucket` |
| The secret | You created it. | `aws secretsmanager delete-secret --secret-id dial/<name> --force-delete-without-recovery` |
| Final database snapshot | Kept by the database stack. | `aws rds delete-db-cluster-snapshot` |
| CloudWatch log groups | EKS and RDS create them. | `aws logs delete-log-group` |
| AWS License Manager service-linked role | Shared by every product in the account; costs nothing. | Leave it. |

### 6. Check that nothing that costs money is left

EKS clusters, NAT gateways, Elastic IPs, VPC endpoints, load balancers, RDS clusters and snapshots, ElastiCache,
EBS volumes and snapshots, S3 buckets, ECR repositories, secrets, log groups.
