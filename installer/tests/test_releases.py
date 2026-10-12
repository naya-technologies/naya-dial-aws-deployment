import copy
import unittest
from pathlib import Path
from unittest import mock

import yaml

from helpers import Base, RELEASE, ROOT, make_ctx
from dial_install import __version__, releases, workflow
from dial_install.errors import InstallerError
from dial_install.runner import FakeRunner, Result


def manifest(**changes):
    data = yaml.safe_load((ROOT / "releases" / f"{RELEASE['release']}.yaml").read_text())
    data.update(changes)
    return data


class BundledTests(Base):
    def test_every_bundled_release_is_valid_and_indexed(self):
        for release_id in releases.bundled_ids():
            loaded = releases.load_bundled(release_id)
            self.assertEqual(loaded["release"], release_id)

    def test_latest_is_one_of_the_bundled_releases(self):
        self.assertIn(releases.bundled_index()["latest"], releases.bundled_ids())

    def test_latest_resolves_without_network(self):
        runner = FakeRunner()
        merged = releases.resolve("latest", make_ctx().cloud.__class__(runner, "us-east-1", None, "k"), login=lambda: None)
        self.assertEqual(merged["release"], releases.bundled_index()["latest"])
        self.assertEqual(merged["release_source"], "package")
        self.assertEqual(runner.calls, [])

    def test_release_carries_every_component_version(self):
        for key in ("chart", "kubernetes", "database", "addons"):
            self.assertIn(key, RELEASE)
        self.assertIn("repository", RELEASE["chart"])          # from installer.yaml
        self.assertIn("version", RELEASE["chart"])             # from the release
        self.assertIn("node_groups", RELEASE)                  # installer defaults survive the merge

    def test_merge_prefers_release_values_and_keeps_defaults(self):
        merged = releases.merge({"a": {"x": 1, "y": 2}, "b": 1}, {"a": {"y": 3}})
        self.assertEqual(merged, {"a": {"x": 1, "y": 3}, "b": 1})


class ValidationTests(Base):
    def test_wrong_api_version(self):
        with self.assertRaises(InstallerError):
            releases.validate(manifest(apiVersion="something/else"), "x")

    def test_missing_component_section(self):
        data = manifest()
        del data["addons"]
        with self.assertRaises(InstallerError) as raised:
            releases.validate(data, "x")
        self.assertIn("addons", raised.exception.message)

    def test_newer_installer_required(self):
        with self.assertRaises(InstallerError) as raised:
            releases.validate(manifest(min_installer_version="99.0.0"), "x")
        self.assertIn("99.0.0", raised.exception.message)
        self.assertIn("Download", raised.exception.hint)

    def test_current_installer_is_accepted(self):
        releases.validate(manifest(min_installer_version=__version__), "x")


class MarketplaceSourceTests(Base):
    def cloud(self, responses):
        return make_ctx(responses=responses).cloud

    def pull_response(self, data):
        def pull(argv):
            target = Path(argv[argv.index("--untardir") + 1]) / "chart"
            target.mkdir()
            (target / "release.yaml").write_text(yaml.safe_dump(data))
            return Result(0)
        return pull

    def test_unbundled_release_is_pulled_and_merged(self):
        logins = []
        cloud = self.cloud({("helm", "pull"): self.pull_response(manifest(release="2030.1", chart={"version": "2030.1"}))})
        merged = releases.resolve("2030.1", cloud, login=lambda: logins.append(1))
        self.assertEqual(merged["chart"]["version"], "2030.1")
        self.assertEqual(merged["release_source"], "registry chart")
        self.assertEqual(logins, [1])
        self.assertIn("node_groups", merged)

    def test_chart_without_manifest(self):
        def empty(argv):
            (Path(argv[argv.index("--untardir") + 1]) / "chart").mkdir()
            return Result(0)
        with self.assertRaises(InstallerError) as raised:
            releases.resolve("2030.1", self.cloud({("helm", "pull"): empty}), login=lambda: None)
        self.assertIn("release.yaml", raised.exception.message)

    def test_manifest_for_another_release_is_rejected(self):
        cloud = self.cloud({("helm", "pull"): self.pull_response(manifest(release="2029.9"))})
        with self.assertRaises(InstallerError):
            releases.resolve("2030.1", cloud, login=lambda: None)

    def test_pull_failure_is_explained(self):
        with self.assertRaises(InstallerError) as raised:
            releases.resolve("2030.1", self.cloud({("helm", "pull"): Result(1, "", "unauthorized")}), login=lambda: None)
        self.assertIn("2030.1", raised.exception.message)
        self.assertIn("unauthorized", raised.exception.hint)


class CommandTests(Base):
    def test_releases_command_lists_notes(self):
        lines = []
        workflow.cmd_releases(None, None, lines.append)
        text = "\n".join(lines)
        self.assertIn(RELEASE["release"], text)
        self.assertIn("(latest)", text)
        self.assertIn("Marketplace", text)


if __name__ == "__main__":
    unittest.main()
