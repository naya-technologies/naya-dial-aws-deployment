"""`dial-install uninstall`: remove everything one installation created, in an order that cannot get stuck.

It acts only on what this installation owns: the CloudFormation stacks named `<name>-<slot>` that carry the
installer's tag, the ECR repositories tagged `dial-install:installation=<name>`, and the objects the installer
named after the installation (secret, log groups, private DNS zone). Nothing else in the account is touched.

Why the order matters:
1. Load balancers are removed while the AWS Load Balancer Controller still runs; afterwards nothing would remove
   them and they would block the network stack.
2. Stacks have no dependencies between them in CloudFormation, so the independent ones are deleted together.
3. The network goes last, after the cluster that used its interfaces.
"""

from __future__ import annotations

import json
import time
from typing import Callable

from .bootstrap import Bootstrap
from .context import Context
from .errors import CommandError

REVISION_TAG = "dial-install:infra-revision"        # set on every stack by infra.deploy
LAST_SLOTS = ("endpoints", "network", "storage")    # deleted after everything else, in this order
Say = Callable[[str], None]


class Uninstaller:
    def __init__(self, ctx: Context, say: Say, *, keep_images: bool = False, keep_secret: bool = False,
                 keep_snapshot: bool = False) -> None:
        self.ctx = ctx
        self.cloud = ctx.cloud
        self.say = say
        self.keep_images = keep_images
        self.keep_secret = keep_secret
        self.keep_snapshot = keep_snapshot
        self.dry_run = ctx.cloud.runner.dry_run
        self._buckets: list[str] = []
        self.namespace = ctx.release["helm_release"]["namespace"]
        self.vpc_id: str | None = None

    # ---- what exists ---------------------------------------------------------------------------
    def owned_stacks(self) -> dict[str, str]:
        """slot -> stack name, for the installation's stacks that exist and carry the installer's tag."""
        found: dict[str, str] = {}
        for module in self.ctx.modules:
            if not module.stack_file:
                continue
            name = self.ctx.stack_name(module.slot)
            data = self.cloud.aws_json("cloudformation", "describe-stacks", "--stack-name", name, check=False)
            if not data or not data.get("Stacks"):
                continue
            tags = {t["Key"] for t in data["Stacks"][0].get("Tags", [])}
            if REVISION_TAG not in tags:
                self.say(f"  skipping stack {name}: it was not created by dial-install")
                continue
            found[module.slot] = name
        return found

    def _outputs(self, stack: str) -> dict[str, str]:
        data = self.cloud.aws_json("cloudformation", "describe-stacks", "--stack-name", stack, check=False)
        if not data or not data.get("Stacks"):
            return {}
        return {o["OutputKey"]: o["OutputValue"] for o in data["Stacks"][0].get("Outputs", [])}

    def _resources(self, stack: str, resource_type: str) -> list[str]:
        data = self.cloud.aws_json("cloudformation", "describe-stack-resources", "--stack-name", stack, check=False)
        return [r["PhysicalResourceId"] for r in (data or {}).get("StackResources", []) if r["ResourceType"] == resource_type]

    def repositories(self) -> list[str]:
        """Mirrored product repositories tagged with this installation."""
        data = self.cloud.aws_json("ecr", "describe-repositories", check=False) or {}
        names = []
        for repo in data.get("repositories", []):
            tags = self.cloud.aws_json("ecr", "list-tags-for-resource", "--resource-arn", repo["repositoryArn"], check=False) or {}
            values = {t["Key"]: t["Value"] for t in tags.get("tags", [])}
            if values.get("dial-install:installation") == self.ctx.name:
                names.append(repo["repositoryName"])
        return names

    # ---- the plan, in plain words --------------------------------------------------------------
    def plan(self) -> list[str]:
        stacks = self.owned_stacks()
        lines = [f"Cluster workloads: DIAL, Knative, Istio, load balancers, volumes (cluster {self.ctx.cluster_name})",
                 f"Private DNS zone {self.namespace}.knative.{self.ctx.name}.internal"]
        lines += [f"CloudFormation stack {name} ({slot})" for slot, name in stacks.items()]
        if not self.keep_images:
            lines.append(f"{len(self.repositories())} mirrored product image repositories in your ECR")
        if not self.keep_secret:
            lines.append(f"Secret {self.ctx.secret_arn.split(':secret:')[-1]}")
        lines.append("Log groups of the cluster and the database")
        if not self.keep_snapshot:
            lines.append("The final database snapshot")
        return lines

    # ---- steps ---------------------------------------------------------------------------------
    def run(self) -> list[str]:
        """Remove everything; returns what is still left (empty when the account is clean)."""
        stacks = self.owned_stacks()
        network = stacks.get("network")
        if network:
            self.vpc_id = self._outputs(network).get("VPCId")
        self.report_license()
        self.cluster_workloads()
        self.private_zone()
        self.database_protection(stacks.get("database"))
        self.delete_stacks(stacks)
        self.mirrored_images()
        self.secret_and_logs()
        return self.leftovers()

    def report_license(self) -> None:
        data = self.cloud.aws_json("license-manager", "list-received-licenses", check=False) or {}
        consumed = []
        for lic in data.get("Licenses", []):
            usage = self.cloud.aws_json("license-manager", "get-license-usage", "--license-arn", lic["LicenseArn"], check=False) or {}
            for entry in usage.get("LicenseUsage", {}).get("EntitlementUsages", []):
                if entry.get("ConsumedValue") not in (None, "0") and entry.get("Name") != "AWS::Marketplace::Usage":
                    consumed.append(f"{entry['Name']} {entry['ConsumedValue']}/{entry.get('MaxCount', '?')}")
        if consumed:
            self.say("  License usage in this account: " + ", ".join(consumed) + ".")
            self.say("  Checkouts of type PERPETUAL cannot be released through the API (License Manager refuses "
                     "CheckInLicense). Contact AWS Marketplace seller support if units must be returned.")
        else:
            self.say("  License usage: nothing consumed.")

    def cluster_workloads(self) -> None:
        self.say("  Removing the workloads and their load balancers")
        exists = self.cloud.aws("eks", "describe-cluster", "--name", self.ctx.cluster_name, check=False)
        if exists.returncode != 0:
            self.say("    the cluster does not exist; skipping")
        else:
            self.cloud.aws("eks", "update-kubeconfig", "--name", self.ctx.cluster_name,
                           "--alias", self.cloud.kube_context, check=False)
            kubectl = self.cloud.kubectl
            kubectl("-n", self.namespace, "delete", "ingress", "--all", "--wait=false", write=True, check=False)
            kubectl("-n", "istio-system", "delete", "svc", "istio-ingressgateway", "--wait=false", write=True, check=False)
            self._wait_for_load_balancers(minutes=5)
            for release, namespace in (("dial", self.namespace), ("cluster-autoscaler", "kube-system"),
                                       ("dial-cluster-base", "kube-system")):
                self.cloud.helm("uninstall", release, "--namespace", namespace, write=True, check=False)
            kubectl("-n", self.namespace, "delete", "pvc", "--all", "--wait=false", write=True, check=False)
            kubectl("delete", "ns", self.namespace, "knative-serving", "istio-system", "--wait=false",
                    write=True, check=False)
        self._remove_orphans()

    def _balancers(self) -> list[dict]:
        if not self.vpc_id:
            return []
        data = self.cloud.aws_json("elbv2", "describe-load-balancers", check=False) or {}
        return [b for b in data.get("LoadBalancers", []) if b.get("VpcId") == self.vpc_id]

    def _wait_for_load_balancers(self, minutes: int) -> None:
        if self.dry_run:
            return
        deadline = time.monotonic() + minutes * 60
        while self._balancers() and time.monotonic() < deadline:
            time.sleep(10)

    def _remove_orphans(self) -> None:
        """Load balancers, target groups and security groups the controller could not remove."""
        for balancer in self._balancers():
            self.cloud.aws("elbv2", "delete-load-balancer", "--load-balancer-arn", balancer["LoadBalancerArn"],
                           write=True, check=False)
        if not self._balancers() or self.dry_run:
            groups = self.cloud.aws_json("elbv2", "describe-target-groups", check=False) or {}
            if not self.dry_run:
                time.sleep(20)
            for group in groups.get("TargetGroups", []):
                if group.get("VpcId") == self.vpc_id and self.vpc_id:
                    self.cloud.aws("elbv2", "delete-target-group", "--target-group-arn", group["TargetGroupArn"],
                                   write=True, check=False)

    def private_zone(self) -> None:
        zone_name = f"{self.namespace}.knative.{self.ctx.name}.internal"
        if not self.vpc_id:
            return
        zone_id = Bootstrap(self.ctx, self.say)._find_private_zone(zone_name, self.vpc_id)
        if not zone_id:
            return
        self.say(f"  Removing the private DNS zone {zone_name}")
        records = self.cloud.aws_json("route53", "list-resource-record-sets", "--hosted-zone-id", zone_id, check=False) or {}
        changes = [{"Action": "DELETE", "ResourceRecordSet": r} for r in records.get("ResourceRecordSets", [])
                   if r["Type"] not in ("NS", "SOA")]
        if changes:
            self.cloud.aws("route53", "change-resource-record-sets", "--hosted-zone-id", zone_id,
                           "--change-batch", json.dumps({"Changes": changes}), write=True, check=False)
        self.cloud.aws("route53", "delete-hosted-zone", "--id", zone_id, write=True, check=False)

    def database_protection(self, stack: str | None) -> None:
        if not stack:
            return
        for cluster in self._resources(stack, "AWS::RDS::DBCluster"):
            self.say(f"  Turning off deletion protection of database {cluster}")
            self.cloud.aws("rds", "modify-db-cluster", "--db-cluster-identifier", cluster, "--no-deletion-protection",
                           "--apply-immediately", write=True, check=False)

    def delete_stacks(self, stacks: dict[str, str]) -> None:
        first = [(slot, name) for slot, name in stacks.items() if slot not in LAST_SLOTS]
        self.say(f"  Deleting {len(first)} stacks together: " + ", ".join(slot for slot, _ in first))
        for _, name in first:
            self.cloud.aws("cloudformation", "delete-stack", "--stack-name", name, write=True, check=False)
        for slot, name in first:
            self._wait_deleted(name)
        self._remove_orphans()
        for slot in LAST_SLOTS:
            name = stacks.get(slot)
            if not name:
                continue
            if slot == "storage":
                self._empty_storage(name)
            self.say(f"  Deleting stack {name}")
            self.cloud.aws("cloudformation", "delete-stack", "--stack-name", name, write=True, check=False)
            self._wait_deleted(name)
            if slot == "storage":
                self._delete_bucket_left_behind(name)

    def _wait_deleted(self, stack: str) -> None:
        if self.dry_run:
            return
        self.cloud.aws("cloudformation", "wait", "stack-delete-complete", "--stack-name", stack, check=False)

    def _empty_storage(self, stack: str) -> None:
        for repo in self._resources(stack, "AWS::ECR::Repository"):
            self.cloud.aws("ecr", "delete-repository", "--repository-name", repo, "--force", write=True, check=False)
        self._buckets = self._resources(stack, "AWS::S3::Bucket")
        for bucket in self._buckets:
            self.cloud.aws("s3", "rm", f"s3://{bucket}", "--recursive", write=True, check=False)

    def _delete_bucket_left_behind(self, stack: str) -> None:
        """The bucket is kept by the stack on purpose (DeletionPolicy: Retain); it is removed here."""
        for bucket in self._buckets:
            self.cloud.aws("s3api", "delete-bucket", "--bucket", bucket, write=True, check=False)

    def mirrored_images(self) -> None:
        if self.keep_images:
            self.say("  Keeping the mirrored images (--keep-images)")
            return
        repos = self.repositories()
        self.say(f"  Deleting {len(repos)} mirrored image repositories")
        for repo in repos:
            self.cloud.aws("ecr", "delete-repository", "--repository-name", repo, "--force", write=True, check=False)

    def secret_and_logs(self) -> None:
        if not self.keep_secret:
            self.say("  Deleting the secret")
            self.cloud.aws("secretsmanager", "delete-secret", "--secret-id", self.ctx.secret_arn,
                           "--force-delete-without-recovery", write=True, check=False)
        cluster = self.ctx.cluster_name
        for prefix in (f"/aws/eks/{cluster}/", f"/aws/containerinsights/{cluster}/", f"/aws/rds/cluster/{self.ctx.name}-"):
            groups = self.cloud.aws_json("logs", "describe-log-groups", "--log-group-name-prefix", prefix, check=False) or {}
            for group in groups.get("logGroups", []):
                self.cloud.aws("logs", "delete-log-group", "--log-group-name", group["logGroupName"], write=True, check=False)
        if not self.keep_snapshot:
            data = self.cloud.aws_json("rds", "describe-db-cluster-snapshots", check=False) or {}
            for snap in data.get("DBClusterSnapshots", []):
                name = snap["DBClusterSnapshotIdentifier"]
                if name.startswith(f"{self.ctx.name}-database-"):
                    self.cloud.aws("rds", "delete-db-cluster-snapshot", "--db-cluster-snapshot-identifier", name,
                                   write=True, check=False)

    # ---- the final check -----------------------------------------------------------------------
    def leftovers(self) -> list[str]:
        """Things of this installation that still exist and may cost money."""
        left: list[str] = []
        for module in self.ctx.modules:
            if module.stack_file:
                name = self.ctx.stack_name(module.slot)
                data = self.cloud.aws_json("cloudformation", "describe-stacks", "--stack-name", name, check=False)
                if data and data.get("Stacks"):
                    left.append(f"stack {name} ({data['Stacks'][0]['StackStatus']})")
        if self.cloud.aws("eks", "describe-cluster", "--name", self.ctx.cluster_name, check=False).returncode == 0:
            left.append(f"EKS cluster {self.ctx.cluster_name}")
        caches = self.cloud.aws_json("elasticache", "describe-serverless-caches", check=False) or {}
        left += [f"ElastiCache {c['ServerlessCacheName']}" for c in caches.get("ServerlessCaches", [])
                 if c["ServerlessCacheName"].startswith(f"{self.ctx.name}-")]
        clusters = self.cloud.aws_json("rds", "describe-db-clusters", check=False) or {}
        left += [f"database {c['DBClusterIdentifier']}" for c in clusters.get("DBClusters", [])
                 if c["DBClusterIdentifier"].startswith(f"{self.ctx.name}-")]
        left += [f"image repository {r}" for r in ([] if self.keep_images else self.repositories())]
        if not self.keep_secret:
            secrets = self.cloud.aws_json("secretsmanager", "list-secrets", "--include-planned-deletion", check=False) or {}
            left += [f"secret {s['Name']}" for s in secrets.get("SecretList", []) if s["ARN"] == self.ctx.secret_arn]
        return left


def uninstall(ctx: Context, say: Say, **options) -> list[str]:
    try:
        return Uninstaller(ctx, say, **options).run()
    except CommandError as error:                       # a failed AWS call: say which, leave the rest for a rerun
        say(f"  stopped: {error}")
        raise
