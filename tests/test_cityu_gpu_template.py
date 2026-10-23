"""Render the cityu-gpu templates and assert the panel stack wiring.

The templates are rendered with plain Jinja2 instead of Ansible's templar, the
same approach as test_clash_template.py, so the test runs without inventories
or secrets. The Grafana dashboard is a plain JSON file: its structure and
every PromQL expression are asserted against the metric names nvitop-exporter
actually exports.
"""

import json
import unittest
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from jinja2 import Environment, FileSystemLoader, StrictUndefined

PLAYBOOKS = Path(__file__).resolve().parents[1] / "playbooks"
TEMPLATE_DIR = PLAYBOOKS / "templates" / "cityu-gpu"
FILES_DIR = PLAYBOOKS / "files" / "cityu-gpu"

EXPORTERS = {"cs11369a": "100.104.99.124", "cs659b": "100.67.212.32"}
EXPORTER_PORT = 8000
PROM_LOCAL_PORT = 50020
GRAFANA_LOCAL_PORT = 50019
DOMAIN = "cityu-gpu.myl.moe"
CERT = "/etc/nginx/tls/cloudflare/myl.moe.pem"
KEY = "/etc/nginx/tls/cloudflare/myl.moe.key"
ADMIN_PASSWORD = "pa$$word"

# Metric names exported by nvitop-exporter (units are part of the name).
KNOWN_METRICS = {
    "gpu_utilization_Percentage",
    "gpu_memory_used_MiB",
    "gpu_memory_total_MiB",
    "gpu_memory_percent_Percentage",
    "gpu_temperature_C",
    "process_gpu_memory_MiB",
    "process_gpu_sm_utilization_Percentage",
}


def render_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        # Ansible's templar trims the newline after block tags; match it so
        # the test renders what the playbook actually deploys.
        trim_blocks=True,
    )


def render_template(name: str, **variables: Any) -> str:
    env = render_env()
    return env.get_template(name).render(**variables)


class ServiceUnitTest(unittest.TestCase):
    def test_exec_start_wiring(self) -> None:
        unit = render_template(
            "nvitop-exporter.service.j2",
            tailnet_ip="100.104.99.124",
            cityu_gpu_exporter_port=EXPORTER_PORT,
            inventory_hostname="cs11369a",
        )
        self.assertIn(
            "--bind-address 100.104.99.124 --port 8000 --hostname cs11369a --interval 1",
            unit,
        )
        self.assertIn("User=nvitop-exporter", unit)
        self.assertIn("Restart=always", unit)


class PrometheusConfigTest(unittest.TestCase):
    def test_targets_from_exporter_map(self) -> None:
        config = yaml.safe_load(
            render_template(
                "prometheus.yml.j2",
                cityu_gpu_exporters=EXPORTERS,
                cityu_gpu_exporter_port=EXPORTER_PORT,
            )
        )
        assert config is not None
        self.assertEqual(config["global"]["scrape_interval"], "1s")
        job = config["scrape_configs"][0]
        self.assertEqual(job["job_name"], "cityu-gpu")
        self.assertEqual(
            job["static_configs"][0]["targets"],
            ["100.104.99.124:8000", "100.67.212.32:8000"],
        )


class ComposeTest(unittest.TestCase):
    def test_stack_wiring(self) -> None:
        compose = yaml.safe_load(
            render_template(
                "compose.yaml.j2",
                cityu_gpu_domain=DOMAIN,
                cityu_gpu_grafana_admin_password=ADMIN_PASSWORD,
                cityu_gpu_prometheus_local_port=PROM_LOCAL_PORT,
                cityu_gpu_grafana_local_port=GRAFANA_LOCAL_PORT,
            )
        )
        assert compose is not None
        prometheus = compose["services"]["prometheus"]
        self.assertEqual(prometheus["image"], "prom/prometheus:latest")
        self.assertEqual(prometheus["restart"], "always")
        self.assertEqual(prometheus["ports"], ["127.0.0.1:50020:9090"])
        self.assertIn("--storage.tsdb.retention.time=30d", prometheus["command"])
        # compose `command` replaces the image CMD, so the full arg set matters
        self.assertIn("--config.file=/etc/prometheus/prometheus.yml", prometheus["command"])
        self.assertIn("--storage.tsdb.path=/prometheus", prometheus["command"])
        grafana = compose["services"]["grafana"]
        self.assertEqual(grafana["image"], "grafana/grafana:latest")
        self.assertEqual(grafana["ports"], ["127.0.0.1:50019:3000"])
        env = grafana["environment"]
        self.assertIn("GF_AUTH_ANONYMOUS_ENABLED=true", env)
        self.assertIn("GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer", env)
        self.assertIn("GF_SERVER_ROOT_URL=https://cityu-gpu.myl.moe", env)
        self.assertIn(
            "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=/var/lib/grafana/dashboards/cityu-gpu.json",
            env,
        )
        # compose consumes $$ as a literal $, so secrets survive the env file
        self.assertIn("GF_SECURITY_ADMIN_PASSWORD=pa$$$$word", env)


class NginxSiteTest(unittest.TestCase):
    def test_site_wiring(self) -> None:
        site = render_template(
            "cityu-gpu.nginx.j2",
            cityu_gpu_domain=DOMAIN,
            cityu_gpu_cert=CERT,
            cityu_gpu_key=KEY,
            cityu_gpu_grafana_local_port=GRAFANA_LOCAL_PORT,
        )
        self.assertIn("server_name cityu-gpu.myl.moe;", site)
        self.assertIn("ssl_certificate /etc/nginx/tls/cloudflare/myl.moe.pem;", site)
        self.assertIn("auth_basic_user_file /etc/nginx/htpasswd/cityu-gpu;", site)
        self.assertIn("proxy_pass http://127.0.0.1:50019;", site)
        self.assertIn("include snippets/rproxy.conf;", site)


class DashboardTest(unittest.TestCase):
    dashboard: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        with (FILES_DIR / "grafana" / "dashboards" / "cityu-gpu.json").open() as f:
            cls.dashboard = json.load(f)

    def test_refresh_and_time_range(self) -> None:
        self.assertEqual(self.dashboard["refresh"], "5s")
        self.assertEqual(self.dashboard["time"], {"from": "now-15m", "to": "now"})

    def test_panels_present(self) -> None:
        types = [panel["type"] for panel in self.dashboard["panels"]]
        self.assertEqual(types.count("stat"), 3)
        self.assertEqual(types.count("table"), 2)
        self.assertEqual(types.count("timeseries"), 2)
        titles = [panel["title"] for panel in self.dashboard["panels"]]
        self.assertIn("空闲 GPU", titles)
        self.assertIn("逐卡状态", titles)
        self.assertIn("逐用户占用", titles)

    def test_queries_reference_known_metrics(self) -> None:
        exprs = [
            target["expr"]
            for panel in self.dashboard["panels"]
            for target in panel.get("targets", [])
        ]
        self.assertGreaterEqual(len(exprs), 8)
        for expr in exprs:
            self.assertTrue(
                any(metric in expr for metric in KNOWN_METRICS),
                f"expression references no known metric: {expr}",
            )
        # The idle count must combine the memory and utilization thresholds,
        # and fall back to 0 when every GPU is busy (count of an empty set
        # yields no sample, which would render as No data instead of 0).
        idle = exprs[0]
        self.assertIn("gpu_memory_used_MiB < 2048", idle)
        self.assertIn("gpu_utilization_Percentage < 10", idle)
        self.assertIn("or vector(0)", idle)

    def test_datasource_uid_matches_provisioning(self) -> None:
        with (
            FILES_DIR / "grafana" / "provisioning" / "datasources" / "prometheus.yml"
        ).open() as f:
            datasource = yaml.safe_load(f)
        assert datasource is not None
        self.assertEqual(
            datasource["datasources"][0]["url"], "http://prometheus:9090"
        )
        uid = datasource["datasources"][0]["uid"]
        for panel in self.dashboard["panels"]:
            self.assertEqual(panel["datasource"]["uid"], uid)


if __name__ == "__main__":
    unittest.main()
