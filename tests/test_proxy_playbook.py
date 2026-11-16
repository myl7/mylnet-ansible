"""Guard stable updates and avoid duplicate proxy recreation."""
import unittest
from pathlib import Path

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]


class ProxyPlaybookTests(unittest.TestCase):
    def test_single_compose_recreation_path(self):
        plays = yaml.safe_load((ROOT / "playbooks/proxy.yaml").read_text())
        play = next(p for p in plays if p["name"] == "Deploy proxy")
        tasks = play["tasks"]
        names = [task["name"] for task in tasks]
        self.assertLess(names.index("Preserve previous mihomo image"), names.index("Start containers"))
        self.assertNotIn("handlers", play)
        for task in tasks:
            self.assertNotEqual(task.get("notify"), "Recreate containers")
        start = next(t for t in tasks if t["name"] == "Start containers")
        compose = start["community.docker.docker_compose_v2"]
        self.assertEqual(compose["pull"], "always")
        self.assertEqual(compose["recreate"], "{{ 'always' if proxy_config is defined and proxy_config is changed else 'auto' }}")
        self.assertTrue(compose["wait"])

    def test_image_only_tag_excludes_network_configuration(self):
        plays = yaml.safe_load((ROOT / "playbooks/proxy.yaml").read_text())
        selected = [task for play in plays for task in play.get("tasks", [])
                    if "mihomo-update" in task.get("tags", [])]
        self.assertEqual(len(selected), 8)
        for task in selected:
            self.assertNotIn("ansible.builtin.copy", task)
            self.assertNotIn("ansible.builtin.template", task)
            self.assertNotIn("ansible.builtin.script", task)
            self.assertNotIn("ansible.builtin.shell", task)
