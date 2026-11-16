"""Guard the Gitea upgrade order without contacting a managed host."""
import unittest
from pathlib import Path

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]


class GiteaUpgradeTest(unittest.TestCase):
    def setUp(self):
        self.play = yaml.safe_load((ROOT / "playbooks/gitea.yaml").read_text())[0]
        self.tasks = self.play["tasks"]

    def test_image_preservation_precedes_compose_update(self):
        names = [task["name"] for task in self.tasks]
        self.assertLess(names.index("Preserve existing Gitea before updating"), names.index("Render compose file"))
        self.assertLess(names.index("Start containers"), names.index("Verify running Gitea"))
        backup = next(t for t in self.tasks if t["name"] == "Preserve existing Gitea before updating")
        self.assertEqual(backup["when"], "not ansible_check_mode")

    def test_float_pull_and_no_deferred_recreate(self):
        compose = (ROOT / "playbooks/templates/gitea/compose.yaml.j2").read_text()
        self.assertIn("image: docker.gitea.com/gitea\n", compose)
        start = next(t for t in self.tasks if t["name"] == "Start containers")
        module = start["community.docker.docker_compose_v2"]
        self.assertEqual(module["pull"], "always")
        self.assertTrue(module["wait"])
        self.assertNotIn("Recreate containers", (ROOT / "playbooks/gitea.yaml").read_text())

    def test_version_floor_and_no_automatic_backup_downtime(self):
        text = (ROOT / "playbooks/gitea.yaml").read_text()
        self.assertIn("version('28.4.0', '>=', version_type='semver')", text)
        self.assertIn("/api/healthz", text)
        self.assertIn("mylnet/gitea:rollback-", text)
        self.assertNotIn("docker, stop", text)
        self.assertNotIn("data.tar.gz", text)
        self.assertNotIn("ansible.builtin.tempfile", text)


if __name__ == "__main__":
    unittest.main()
