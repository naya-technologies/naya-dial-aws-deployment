"""Contract tests: every module must be internally consistent with its CloudFormation template."""

import copy
import unittest

import yaml

from helpers import Base, FAKE_OUTPUTS, ROOT, example, make_ctx, ok_json, resolved
from dial_install import config, infra, modules
from dial_install.errors import InstallerError
from dial_install.runner import Result


class _CfnLoader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {suffix: loader.construct_sequence(node, deep=True)}
    return {suffix: loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag)


def template(module):
    return yaml.load(module.stack_file.read_text(), Loader=_CfnLoader)


def refs(node):
    """Every !Ref / !Sub variable / !GetAtt target used in a template fragment."""
    found = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Ref":
                found.add(value)
            elif key == "Sub":
                text = value if isinstance(value, str) else value[0]
                local = set(value[1]) if isinstance(value, list) and len(value) > 1 else set()   # Sub's own variables
                import re
                found.update(m.split(".")[0] for m in re.findall(r"\$\{([^}!]+)\}", text) if m not in local)
            elif key == "GetAtt":
                found.add(value.split(".")[0] if isinstance(value, str) else value[0])
            elif key == "Condition" and isinstance(value, str):
                pass
            found |= refs(value)
    elif isinstance(node, list):
        for item in node:
            found |= refs(item)
    return found


class DiscoveryTests(Base):
    def test_all_modules_load(self):
        found = {m.label for m in modules.discover()}
        self.assertTrue({"network/create", "network/existing-vpc", "compute/eks", "identity/cognito"} <= found)

    def test_default_selection_order(self):
        order = [m.slot for m in modules.select(resolved())]
        self.assertEqual(order, ["network", "endpoints", "storage", "compute", "cni", "links", "cache", "database", "iam", "identity", "registry"])

    def test_every_requirement_precedes_its_user(self):
        chosen = modules.select(resolved())
        seen = set()
        for module in chosen:
            self.assertTrue(set(module.requires) <= seen, module.label)
            seen.add(module.slot)

    def test_provider_choice_picks_module(self):
        cfg = config.resolve(example("full"), ctx_release())
        self.assertEqual(next(m for m in modules.select(cfg) if m.slot == "network").directory.name, "existing-vpc")
        self.assertEqual(next(m for m in modules.select(cfg) if m.slot == "identity").directory.name, "cognito")

    def test_unknown_provider(self):
        cfg = resolved()
        cfg["network"]["provider"] = "teleport"
        with self.assertRaises(InstallerError):
            modules.select(cfg)

    def test_missing_slot_for_requirement(self):
        only = {m.slot: m for m in modules.discover() if m.slot == "compute"}
        with self.assertRaises(InstallerError):
            modules.order(only)


def ctx_release():
    from helpers import RELEASE
    return RELEASE


class StackContractTests(Base):
    """Parameters, outputs and size of every template match what the module declares."""

    def all_stack_modules(self):
        return [m for m in modules.discover() if m.stack_file]

    def test_templates_fit_inline_limit(self):
        for module in self.all_stack_modules():
            self.assertLess(module.stack_file.stat().st_size, 51_200, module.label)

    def test_parameters_match_template(self):
        for module in self.all_stack_modules():
            cfg = resolved() if module.directory.name != "existing-vpc" else config.resolve(example("full"), ctx_release())
            ctx = make_ctx(cfg=cfg)
            ctx.cache["subnets"] = {}
            if module.directory.name == "existing-vpc":
                continue            # parameters are trivial and covered below
            declared = template(module).get("Parameters", {})
            given = module.parameters(ctx)
            unknown = set(given) - set(declared)
            self.assertFalse(unknown, f"{module.label} passes unknown parameters {unknown}")
            required = {name for name, spec in declared.items() if "Default" not in spec}
            self.assertFalse(required - set(given), f"{module.label} misses required parameters {required - set(given)}")

    def test_existing_vpc_parameters(self):
        module = next(m for m in modules.discover() if m.directory.name == "existing-vpc")
        ctx = make_ctx(cfg=config.resolve(example("full"), ctx_release()))
        declared = template(module)["Parameters"]
        self.assertEqual(set(module.parameters(ctx)), set(declared))

    def test_every_reference_is_defined(self):
        for module in self.all_stack_modules():
            tpl = template(module)
            defined = set(tpl.get("Parameters", {})) | set(tpl.get("Resources", {})) | {
                "AWS::StackName", "AWS::Region", "AWS::AccountId", "AWS::NoValue", "AWS::Partition", "AWS::URLSuffix"}
            used = refs(tpl.get("Resources", {})) | refs(tpl.get("Outputs", {}))
            self.assertFalse({u for u in used if not u.startswith("AWS::")} - defined, module.label)
            for name, spec in tpl.get("Resources", {}).items():
                condition = spec.get("Condition")
                if condition:
                    self.assertIn(condition, tpl.get("Conditions", {}), f"{module.label}:{name}")

    def test_declared_outputs_exist(self):
        for module in self.all_stack_modules():
            produced = set(template(module).get("Outputs", {}))
            derived = {"AlbSubnetIds", "PublicSubnet1Id", "PublicSubnet2Id", "PrivateSubnet1Id", "PrivateSubnet2Id",
                       "PrivateRouteTableId", "VPCId"} if module.slot == "network" else set()
            missing = set(module.provides) - produced - derived
            self.assertFalse(missing, f"{module.label} promises outputs the template does not have: {missing}")

    def test_no_secret_in_template_outputs(self):
        for module in self.all_stack_modules():
            for name in template(module).get("Outputs", {}):
                self.assertNotRegex(name, r"(?i)secret$|password", f"{module.label}:{name}")

    def test_database_template_reads_password_from_customer_secret(self):
        module = next(m for m in modules.discover() if m.slot == "database")
        text = module.stack_file.read_text()
        self.assertIn("{{resolve:secretsmanager:${DBSecretArn}:SecretString:db_master_password}}", text)
        self.assertNotIn("DBMasterPassword", text)

    def test_nodes_can_reach_endpoints_before_the_cluster_security_group_exists(self):
        """EKS attaches its own security group to the nodes, and it does not exist until the nodes boot.
        They must reach the EC2 endpoint through the VPC range or they never join the cluster."""
        module = next(m for m in modules.discover() if m.label == "network/create")
        rule = template(module)["Resources"]["VPCESecurityGroupIngress"]["Properties"]
        self.assertEqual(rule["CidrIp"], {"Ref": "VPCCidr"})
        self.assertNotIn("SourceSecurityGroupId", rule)


class NetworkProviderContractTests(Base):
    """`create` and `existing-vpc` must be interchangeable for everything downstream."""

    def test_same_outputs(self):
        by_name = {m.directory.name: m for m in modules.discover() if m.slot == "network"}
        self.assertEqual(set(by_name["create"].provides), set(by_name["existing-vpc"].provides))
        self.assertEqual(set(by_name["create"].provides), set(FAKE_OUTPUTS["network"]))

    def test_create_finalize_picks_subnets_by_exposure(self):
        module = next(m for m in modules.discover() if m.slot == "network" and m.directory.name == "create")
        raw = {k: v for k, v in FAKE_OUTPUTS["network"].items() if k != "AlbSubnetIds"}
        internal = module.finalize_outputs(make_ctx(), raw)
        self.assertEqual(internal["AlbSubnetIds"], "subnet-prv1,subnet-prv2")
        public = module.finalize_outputs(make_ctx(cfg=resolved(network={"exposure": "public"})), raw)
        self.assertEqual(public["AlbSubnetIds"], "subnet-pub1,subnet-pub2")

    def test_existing_vpc_finalize_with_aws_answers(self):
        cfg = config.resolve(example("full"), ctx_release())
        subnets = [
            {"SubnetId": s, "VpcId": "vpc-0123456789abcdef0", "AvailabilityZone": az, "AvailableIpAddressCount": 200}
            for s, az in (("subnet-0aaa111", "a"), ("subnet-0bbb222", "b"), ("subnet-0ccc333", "a"), ("subnet-0ddd444", "b"))
        ]
        runner_answers = {
            ("aws", "ec2", "describe-subnets"): ok_json({"Subnets": subnets}),
            ("aws", "ec2", "describe-route-tables"): ok_json({"RouteTables": [
                {"RouteTableId": "rtb-9", "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-1"}]}]}),
        }
        ctx = make_ctx(cfg=cfg, responses=runner_answers)
        module = next(m for m in modules.discover() if m.directory.name == "existing-vpc")
        out = module.finalize_outputs(ctx, {k: "x" for k in ("ALBSecurityGroupId", "EKSSecurityGroupId", "RDSSecurityGroupId",
                                                               "RedisSecurityGroupId", "VPCESecurityGroupId")})
        self.assertEqual(out["PrivateSubnet1Id"], "subnet-0aaa111")
        self.assertEqual(out["PrivateSubnet2Id"], "subnet-0bbb222")
        self.assertEqual(out["PrivateRouteTableId"], "rtb-9")
        self.assertEqual(out["AlbSubnetIds"], "subnet-0aaa111,subnet-0bbb222")   # internal -> private subnets
        self.assertFalse(set(module.provides) - set(out))

    def test_existing_vpc_rejects_same_az_subnets(self):
        cfg = config.resolve(example("full"), ctx_release())
        subnets = [{"SubnetId": s, "VpcId": "vpc-0123456789abcdef0", "AvailabilityZone": "a", "AvailableIpAddressCount": 200}
                   for s in ("subnet-0aaa111", "subnet-0bbb222", "subnet-0ccc333", "subnet-0ddd444")]
        ctx = make_ctx(cfg=cfg, responses={
            ("aws", "ec2", "describe-vpcs"): ok_json({"Vpcs": [{"VpcId": "vpc-0123456789abcdef0"}]}),
            ("aws", "ec2", "describe-vpc-attribute"): ok_json({"EnableDnsSupport": {"Value": True}, "EnableDnsHostnames": {"Value": True}}),
            ("aws", "ec2", "describe-subnets"): ok_json({"Subnets": subnets}),
        })
        module = next(m for m in modules.discover() if m.directory.name == "existing-vpc")
        levels = [(f.level, f.message) for f in module.check(ctx)]
        self.assertTrue(any(level == "fail" and "Availability Zones" in msg for level, msg in levels), levels)


class EndpointSlotTests(Base):
    def test_endpoints_follow_network_setting(self):
        by = lambda cfg: next(m for m in modules.select(cfg) if m.slot == "endpoints").directory.name
        self.assertEqual(by(resolved()), "create")                                   # new VPC: created
        self.assertEqual(by(config.resolve(example("full"), ctx_release())), "none")  # existing VPC: not created
        self.assertEqual(by(resolved(network={"create_vpc_endpoints": False})), "none")

    def test_none_provider_deploys_nothing(self):
        none = next(m for m in modules.discover() if m.slot == "endpoints" and m.directory.name == "none")
        self.assertIsNone(none.stack_file)


class LicenseManagerRoleTests(Base):
    """License Manager rejects every call until its service-linked role exists; DIAL Core then finds no license."""

    def run_hook(self, get_role):
        ctx = make_ctx(responses={("aws", "iam", "get-role"): get_role})
        module = next(m for m in modules.discover() if m.label == "iam/default" or m.slot == "iam")
        module.before_platform(ctx, lambda _m: None)
        return ctx.cloud.runner

    def test_missing_role_is_created(self):
        runner = self.run_hook(Result(254, "", "NoSuchEntity"))
        self.assertTrue(any("create-service-linked-role" in " ".join(w) and "license-manager.amazonaws.com" in " ".join(w)
                            for w in runner.writes))

    def test_existing_role_is_left_alone(self):
        runner = self.run_hook(ok_json({"Role": {}}))
        self.assertEqual(runner.writes, [])


class InfraTests(Base):
    def test_blocking_stack_states(self):
        for state, blocked in (("ROLLBACK_COMPLETE", True), ("CREATE_IN_PROGRESS", True), ("UPDATE_COMPLETE", False)):
            ctx = make_ctx(responses={("aws", "cloudformation", "describe-stacks"): ok_json({"Stacks": [{"StackStatus": state}]})})
            found = infra.state_findings(ctx, modules.select(ctx.cfg)[:1])
            self.assertEqual(bool(found), blocked, state)

    def test_deploy_dry_run_prints_commands_and_never_writes(self):
        ctx = make_ctx(dry_run=True, with_outputs=False)
        lines = []
        infra.deploy(ctx, modules.select(ctx.cfg), lines.append)
        deploys = ctx.cloud.runner.called("aws", "cloudformation", "deploy")
        self.assertEqual(len(deploys), 9)
        self.assertEqual([c[c.index("--stack-name") + 1] for c in deploys],
                         [f"acme-{s}" for s in ("network", "endpoints", "storage", "compute", "links", "cache", "database", "iam", "identity")])
        for call in deploys:
            self.assertIn("--region", call)
            self.assertIn("CAPABILITY_NAMED_IAM", call)
        self.assertNotIn("Password", " ".join(" ".join(c) for c in deploys))

    def test_database_stack_receives_secret_arn_not_password(self):
        ctx = make_ctx(dry_run=True, with_outputs=False)
        infra.deploy(ctx, modules.select(ctx.cfg), lambda _m: None)
        db = next(c for c in ctx.cloud.runner.called("aws", "cloudformation", "deploy") if "acme-database" in c)
        self.assertIn(f"DBSecretArn={ctx.secret_arn}", db)
        self.assertFalse(any(ctx.secret["db_master_password"] in part for part in db))


if __name__ == "__main__":
    unittest.main()


class SettingsReachTheTemplatesTests(Base):
    def params(self, slot, cfg):
        module = next(m for m in modules.select(cfg) if m.slot == slot)
        ctx = make_ctx(cfg=cfg)
        ctx.cache["subnets"] = {}
        return module.parameters(ctx)

    def test_up_to_five_allowed_networks(self):
        cfg = resolved(network={"allowed_cidrs": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]})
        params = self.params("network", cfg)
        self.assertEqual([params[k] for k in ("AllowedCidr", "AllowedCidr2", "AllowedCidr3", "AllowedCidr4", "AllowedCidr5")],
                         ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "", ""])

    def test_default_is_open_to_the_listed_default_network(self):
        self.assertEqual(self.params("network", resolved())["AllowedCidr"], "0.0.0.0/0")

    def test_database_settings(self):
        params = self.params("database", resolved(database={"backup_retention_days": 21, "deletion_protection": False}))
        self.assertEqual((params["BackupRetentionDays"], params["DeletionProtection"]), ("21", "false"))
        defaults = self.params("database", resolved())
        self.assertEqual((defaults["BackupRetentionDays"], defaults["DeletionProtection"]), ("7", "true"))
        self.assertEqual((defaults["MinCapacity"], defaults["MaxCapacity"]), ("1", "4"))   # a modest ceiling, not 128 ACU

    def test_cluster_settings(self):
        params = self.params("compute", resolved(kubernetes={"public_endpoint": False}, features={"container_logs": False}))
        self.assertEqual((params["PublicEndpoint"], params["EnableContainerLogs"]), ("false", "false"))

    def test_private_endpoint_is_flagged(self):
        module = next(m for m in modules.select(resolved()) if m.slot == "compute")
        ctx = make_ctx(cfg=resolved(kubernetes={"public_endpoint": False}), responses={
            ("aws", "ec2", "describe-instance-type-offerings"): ok_json({"InstanceTypeOfferings": [
                {"InstanceType": "c6i.xlarge"}, {"InstanceType": "c6i.large"}]})})
        text = " ".join(f.message for f in module.check(ctx))
        self.assertIn("private", text)

    def test_reserved_tag_keys_and_bad_cidrs_are_rejected(self):
        cfg = resolved(tags={"dial-install:x": "1", "aws:y": "1"}, network={"allowed_cidrs": ["not-a-cidr"]})
        problems = " | ".join(config.semantic_errors(cfg))
        self.assertIn("dial-install:x", problems)
        self.assertIn("aws:y", problems)
        self.assertIn("not-a-cidr", problems)

    def test_iam_trust_policies_are_conditioned_for_every_role(self):
        module = next(m for m in modules.discover() if m.slot == "iam")
        roles = template(module)["Resources"]
        for name, resource in roles.items():
            if resource["Type"] != "AWS::IAM::Role":
                continue
            document = resource["Properties"]["AssumeRolePolicyDocument"]
            text = document["Sub"][0] if isinstance(document, dict) and "Sub" in document else str(document)
            self.assertIn("sub", text, f"{name} must restrict which service account may assume it")
            self.assertIn("aud", text, f"{name} must restrict the audience")

    def test_core_role_cannot_reach_other_buckets(self):
        module = next(m for m in modules.discover() if m.slot == "iam")
        core = template(module)["Resources"]["CoreServiceRole"]["Properties"]
        self.assertNotIn("ManagedPolicyArns", core)                         # no AmazonS3FullAccess
        self.assertTrue(any(p["PolicyName"] == "DialBucketAccess" for p in core["Policies"]))


class CiliumTests(Base):
    """The overlay network option: two-phase cluster, pod range checks and the platform values it needs."""

    def cfg(self, **kube):
        return resolved(kubernetes={"cni": "cilium", **kube})

    def test_default_has_no_cilium_stack_changes(self):
        ctx = make_ctx()
        module = next(m for m in modules.select(ctx.cfg) if m.slot == "compute")
        self.assertEqual(module.deploy_phases(ctx), [{"parameters": {"Phase": "final"}}])
        self.assertEqual(module.parameters(ctx)["Cni"], "vpc-cni")

    def test_new_cilium_cluster_is_bootstrapped_then_finished(self):
        ctx = make_ctx(cfg=self.cfg(), responses={("aws", "cloudformation", "describe-stacks"): Result(254, "", "does not exist")})
        module = next(m for m in modules.select(ctx.cfg) if m.slot == "compute")
        phases = module.deploy_phases(ctx)
        self.assertEqual([p["parameters"]["Phase"] for p in phases], ["bootstrap", "final"])
        self.assertIsNotNone(phases[0]["after"])

    def test_cluster_with_nodes_is_never_sent_back_to_bootstrap(self):
        stack = {"Stacks": [{"Parameters": [{"ParameterKey": "Phase", "ParameterValue": "final"}]}]}
        ctx = make_ctx(cfg=self.cfg(), responses={("aws", "cloudformation", "describe-stacks"): ok_json(stack)})
        module = next(m for m in modules.select(ctx.cfg) if m.slot == "compute")
        self.assertEqual([p["parameters"]["Phase"] for p in module.deploy_phases(ctx)], ["final"])

    def test_system_nodes_default_to_two_and_logs_off(self):
        cfg = self.cfg()
        self.assertEqual(cfg["sizing"]["node_groups"]["system"]["desired"], 2)
        self.assertFalse(cfg["features"]["container_logs"])

    def test_pod_range_must_not_overlap_the_vpc_or_services(self):
        for cidr in ("10.0.0.0/20", "172.20.0.0/16"):
            problems = config.semantic_errors(self.cfg(pod_cidr=cidr))
            self.assertTrue(any("overlaps" in p for p in problems), cidr)
        self.assertEqual(config.semantic_errors(self.cfg()), [])

    def test_pod_range_only_with_cilium(self):
        problems = config.semantic_errors(resolved(kubernetes={"pod_cidr": "100.96.0.0/18"}))
        self.assertTrue(any("only valid" in p for p in problems))

    def test_every_pod_targeting_ingress_becomes_an_instance_target(self):
        from dial_install import cilium
        chart = yaml.safe_load((ROOT.parent / "charts" / "dial-platform" / "values.yaml").read_text())
        values = cilium.platform_values(chart)
        found = []

        def walk(node, path=""):
            if isinstance(node, dict):
                if node.get("ingress", {}).get("annotations", {}).get(cilium.TARGET_TYPE):
                    found.append(path)
                for key, value in node.items():
                    walk(value, f"{path}.{key}" if path else key)
        walk(values)
        self.assertGreaterEqual(len(found), 6)                      # the six DIAL ingresses
        for path in found:
            node = values
            for key in path.split("."):
                node = node[key]
            self.assertEqual(node["ingress"]["annotations"][cilium.TARGET_TYPE], "instance")
            self.assertEqual(node["service"]["type"], "NodePort")
        self.assertTrue(values["albcontroller"]["hostNetwork"])

    def test_helm_values_use_overlay_and_keep_kube_proxy(self):
        from dial_install import cilium
        ctx = make_ctx(cfg=self.cfg())
        ctx.outputs["compute"]["ClusterEndpoint"] = "https://abc.gr7.us-east-1.eks.amazonaws.com"
        ctx.cache["account_id"] = "123456789012"
        values = cilium.helm_values(ctx)
        self.assertEqual(values["dnsPolicy"], "Default")                  # not the cluster DNS: it needs Cilium first
        self.assertEqual(values["operator"]["dnsPolicy"], "Default")
        self.assertEqual(values["routingMode"], "tunnel")
        self.assertFalse(values["kubeProxyReplacement"])
        self.assertEqual(values["ipam"]["operator"]["clusterPoolIPv4PodCIDRList"], ["100.96.0.0/18"])
        self.assertEqual(values["k8sServiceHost"], "abc.gr7.us-east-1.eks.amazonaws.com")
        self.assertTrue(values["image"]["repository"].startswith("123456789012.dkr.ecr."))
        self.assertTrue(values["operator"]["image"]["override"].endswith("naya-dial-cilium-operator:v1.19.6"))


class CiliumDeployTests(Base):
    def test_dry_run_deploys_control_plane_then_installs_cilium_then_nodes(self):
        from unittest import mock
        from dial_install import tools
        ctx = make_ctx(cfg=resolved(kubernetes={"cni": "cilium"}), dry_run=True, with_outputs=False)
        lines = []
        with mock.patch.object(tools, "which", side_effect=lambda t: f"/usr/bin/{t}"):
            infra.deploy(ctx, modules.select(ctx.cfg), lines.append)
        text = "\n".join(lines)
        compute = [c for c in ctx.cloud.runner.called("aws", "cloudformation", "deploy") if "acme-compute" in c]
        phases = [next(a for a in c if a.startswith("Phase=")) for c in compute]
        self.assertEqual(phases, ["Phase=bootstrap", "Phase=final"])
        self.assertTrue(all("Cni=cilium" in c for c in compute))
        self.assertIn("Installing Cilium", text)
        self.assertLess(text.index("Installing Cilium"), len(text))


class CiliumOrderTests(Base):
    def test_webhooks_move_to_the_node_network_before_any_knative_object_is_changed(self):
        from unittest import mock
        from dial_install import mirror, tools, values
        from dial_install.bootstrap import Bootstrap
        from helpers import CHART_VALUES_TEXT
        from dial_install.runner import FakeRunner
        chart = yaml.safe_load(CHART_VALUES_TEXT)
        ctx = make_ctx(cfg=resolved(kubernetes={"cni": "cilium"}))
        ctx.cloud.runner = FakeRunner({("helm", "show", "values"): Result(0, CHART_VALUES_TEXT), ("kubectl",): Result(0, "{}")})
        with mock.patch.object(tools, "which", side_effect=lambda t: f"/usr/bin/{t}"), \
                mock.patch.object(Bootstrap, "knative_private_dns", lambda self: None), mock.patch("time.sleep"):
            bootstrap = Bootstrap(ctx, lambda _m: None)
            bootstrap.images = mirror.addons(values.effective(ctx, chart))
            bootstrap.knative_with_istio()
        writes = [" ".join(w) for w in ctx.cloud.runner.writes]
        host = next(i for i, w in enumerate(writes) if "patch deployment webhook" in w and "hostNetwork" in w)
        waited = next(i for i, w in enumerate(writes) if "rollout status deployment/webhook" in w)
        first_configmap = next(i for i, w in enumerate(writes) if "patch configmap/config-autoscaler" in w)
        self.assertLess(host, waited)
        self.assertLess(waited, first_configmap)
