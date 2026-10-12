import copy
import unittest

from helpers import Base, RELEASE, example, resolved
from dial_install import config
from dial_install.errors import InstallerError
from dial_install.schema import validate


class SchemaTests(Base):
    def setUp(self):
        self.schema = config.load_schema()

    def test_examples_are_valid(self):
        for name in ("minimal", "full"):
            self.assertEqual(validate(example(name), self.schema), [], name)

    def test_missing_required(self):
        raw = example()
        del raw["secrets"]
        self.assertIn("secrets: is required", validate(raw, self.schema))

    def test_unknown_setting_is_reported(self):
        raw = example()
        raw["netwrk"] = {}
        self.assertIn("netwrk: unknown setting", validate(raw, self.schema))

    def test_existing_vpc_needs_ids(self):
        raw = example()
        raw["network"] = {"provider": "existing-vpc"}
        problems = validate(raw, self.schema)
        self.assertIn("network.vpc_id: is required", problems)
        self.assertIn("network.subnets: is required", problems)

    def test_saml_provider_needs_saml_block(self):
        raw = example()
        raw["identity"] = {"provider": "cognito-saml"}
        self.assertIn("identity.saml: is required", validate(raw, self.schema))

    def test_domain_needs_base_or_all_hosts(self):
        raw = example()
        del raw["domain"]["base"]
        self.assertTrue(validate(raw, self.schema))
        raw["domain"]["hosts"] = {"chat": "chat.example.org"}
        self.assertTrue(validate(raw, self.schema))

    def test_bad_enum_lists_choices(self):
        raw = example()
        raw["network"] = {"exposure": "everywhere"}
        self.assertTrue(any("internal, public" in p for p in validate(raw, self.schema)))


class LoadTests(Base):
    def test_placeholder_is_rejected_with_path(self):
        raw = example()
        raw["secrets"]["arn"] = "TODO: paste the ARN"
        path = self._write(raw)
        with self.assertRaises(InstallerError) as raised:
            config.load(path)
        self.assertIn("secrets.arn", raised.exception.message)

    def _write(self, raw):
        import tempfile, yaml, pathlib
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        yaml.safe_dump(raw, handle)
        handle.close()
        self.addCleanup(lambda: pathlib.Path(handle.name).unlink())
        return pathlib.Path(handle.name)


class ResolveTests(Base):
    def test_defaults(self):
        cfg = resolved()
        self.assertEqual(cfg["network"]["provider"], "create")
        self.assertEqual(cfg["network"]["exposure"], "internal")
        self.assertTrue(cfg["network"]["create_vpc_endpoints"])
        self.assertEqual(cfg["network"]["subnet_cidrs"]["private"], ["10.0.10.0/24", "10.0.11.0/24"])
        self.assertEqual(cfg["kubernetes"]["version"], RELEASE["kubernetes"]["default_version"])
        self.assertEqual(cfg["sizing"]["node_groups"]["dynamic"]["desired"], 3)
        self.assertEqual(cfg["release"], RELEASE["release"])
        self.assertTrue(cfg["features"]["analytics"])

    def test_hosts_derive_from_base(self):
        hosts = resolved()["domain"]["hosts"]
        self.assertEqual(hosts["chat"], "chat.dial.acme.example")
        self.assertEqual(set(hosts), set(config.SERVICES))

    def test_single_host_override(self):
        raw = example()
        raw["domain"]["hosts"] = {"chat": "ai.other.org"}
        hosts = config.resolve(raw, RELEASE)["domain"]["hosts"]
        self.assertEqual(hosts["chat"], "ai.other.org")
        self.assertEqual(hosts["admin"], "admin.dial.acme.example")

    def test_node_group_override_merges(self):
        cfg = resolved(sizing={"node_groups": {"core": {"instance_type": "m6i.2xlarge", "desired": 2, "max": 2}}})
        core = cfg["sizing"]["node_groups"]["core"]
        self.assertEqual((core["instance_type"], core["desired"], core["disk_gb"]), ("m6i.2xlarge", 2, 200))

    def test_existing_vpc_does_not_create_endpoints_by_default(self):
        cfg = config.resolve(example("full"), RELEASE)
        self.assertFalse(cfg["network"]["create_vpc_endpoints"])
        self.assertNotIn("cidr", cfg["network"])


class SemanticTests(Base):
    def test_clean_examples(self):
        self.assertEqual(config.semantic_errors(resolved()), [])
        self.assertEqual(config.semantic_errors(config.resolve(example("full"), RELEASE)), [])

    def test_arn_region_must_match(self):
        cfg = resolved()
        cfg["secrets"]["arn"] = cfg["secrets"]["arn"].replace("us-east-1", "eu-west-1")
        self.assertTrue(any("secrets.arn" in p for p in config.semantic_errors(cfg)))

    def test_node_group_ordering(self):
        cfg = resolved(sizing={"node_groups": {"rag": {"min": 3, "desired": 1, "max": 2}}})
        self.assertTrue(any("min <= desired <= max" in p for p in config.semantic_errors(cfg)))

    def test_disk_only_for_core(self):
        cfg = resolved(sizing={"node_groups": {"rag": {"disk_gb": 100}}})
        self.assertTrue(any("disk_gb" in p for p in config.semantic_errors(cfg)))

    def test_non_16_cidr_needs_subnets(self):
        cfg = config.resolve({**copy.deepcopy(example()), "network": {"cidr": "10.8.0.0/20"}}, RELEASE)
        self.assertTrue(any("subnet_cidrs" in p for p in config.semantic_errors(cfg)))

    def test_overlapping_subnets(self):
        cfg = resolved(network={"cidr": "10.8.0.0/20", "subnet_cidrs": {
            "public": ["10.8.0.0/24", "10.8.0.0/24"], "private": ["10.8.2.0/24", "10.8.3.0/24"]}})
        self.assertTrue(any("overlaps" in p for p in config.semantic_errors(cfg)))

    def test_duplicate_hosts(self):
        raw = example()
        raw["domain"]["hosts"] = {"chat": "same.example.org", "admin": "same.example.org"}
        self.assertTrue(any("distinct" in p for p in config.semantic_errors(config.resolve(raw, RELEASE))))

    def test_public_exposure_needs_public_subnets(self):
        raw = copy.deepcopy(example("full"))
        raw["network"]["exposure"] = "public"
        del raw["network"]["subnets"]["public"]
        self.assertTrue(any("public subnets" in p for p in config.semantic_errors(config.resolve(raw, RELEASE))))


if __name__ == "__main__":
    unittest.main()
