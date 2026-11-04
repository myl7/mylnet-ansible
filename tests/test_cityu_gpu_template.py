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

# Metric names exported by nvitop-exporter (units are part of the name), plus
# the recording rule Prometheus derives from them.
KNOWN_METRICS = {
    "gpu_utilization_Percentage",
    "gpu_memory_used_MiB",
    "gpu_memory_total_MiB",
    "gpu_memory_percent_Percentage",
    "gpu_temperature_C",
    "process_gpu_memory_MiB",
    "process_gpu_sm_utilization_Percentage",
    "gpu_idle",
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
        self.assertEqual(
            config["rule_files"], ["/etc/prometheus/prometheus-rules.yml"]
        )
        job = config["scrape_configs"][0]
        self.assertEqual(job["job_name"], "cityu-gpu")
        self.assertEqual(
            job["static_configs"][0]["targets"],
            ["100.104.99.124:8000", "100.67.212.32:8000"],
        )


class PrometheusRulesTest(unittest.TestCase):
    def test_idle_rule(self) -> None:
        with (FILES_DIR / "prometheus-rules.yml").open() as f:
            rules = yaml.safe_load(f)
        assert rules is not None
        rule = rules["groups"][0]["rules"][0]
        self.assertEqual(rule["record"], "gpu_idle")
        # The idle verdict's single source: the dashboard stat and the public
        # agent query both read gpu_idle, so the thresholds live only here.
        self.assertEqual(
            rule["expr"],
            "gpu_memory_used_MiB < 2048 and gpu_utilization_Percentage < 10",
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
        self.assertIn(
            "./prometheus-rules.yml:/etc/prometheus/prometheus-rules.yml:ro",
            prometheus["volumes"],
        )
        grafana = compose["services"]["grafana"]
        self.assertEqual(grafana["image"], "grafana/grafana:latest")
        self.assertEqual(grafana["ports"], ["127.0.0.1:50019:3000"])
        env = grafana["environment"]
        # No anonymous access: the panel is public but behind a real login.
        self.assertIn("GF_USERS_ALLOW_SIGN_UP=false", env)
        # The agent-query panel's copy button uses an inline onclick, which the
        # text panel sanitizer would strip.
        self.assertIn("GF_PANELS_DISABLE_SANITIZE_HTML=true", env)
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
        # Auth lives in Grafana's own login; nginx basic auth in front of it
        # caused password-prompt storms on every auto refresh.
        self.assertNotIn("auth_basic", site)
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
        self.assertEqual(types.count("text"), 1)
        titles = [panel["title"] for panel in self.dashboard["panels"]]
        self.assertIn("空闲 GPU", titles)
        self.assertIn("逐卡状态", titles)
        self.assertIn("逐用户占用", titles)
        self.assertIn("Agent 查询", titles)

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
        # The idle stat reads the recording rule so the panel and the public
        # agent query share one idle verdict, and falls back to 0 when every
        # GPU is busy (count of an empty set yields no sample, which would
        # render as No data instead of 0).
        idle = exprs[0]
        self.assertEqual(idle, "count(gpu_idle) or vector(0)")

    def test_agent_query_panel_carries_the_copyable_command(self) -> None:
        panel = next(
            p for p in self.dashboard["panels"] if p["title"] == "Agent 查询"
        )
        self.assertEqual(panel["type"], "text")
        options = panel["options"]
        # HTML mode: the panel ships a real copy button whose inline onclick
        # the sanitizer would strip, paired with an unsanitized-HTML env var
        # asserted in ComposeTest.
        self.assertEqual(options["mode"], "html")
        content = options["content"]
        self.assertIn('id="gpu-agent-cmd"', content)
        self.assertIn("onclick=", content)
        self.assertIn("navigator.clipboard.writeText", content)
        # The copied block is addressed to the agent, which runs the command
        # itself: instruction, curl, and how to read the response.
        self.assertIn("帮我查", content)
        self.assertIn(
            "curl -s -u \"USER:PASSWORD\" 'https://cityu-gpu.myl.moe/"
            "api/datasources/proxy/uid/prometheus/api/v1/query"
            "?query=count(gpu_idle)%20or%20vector(0)'",
            content,
        )
        self.assertIn("data.result[0].value[1]", content)
        # Credentials stay with the user: the panel ships a placeholder only.
        self.assertIn("USER:PASSWORD", content)
        self.assertNotIn(ADMIN_PASSWORD, content)

    def test_table_value_columns_are_renamed(self) -> None:
        # Multi-target tables name their value fields "Value #A"; a
        # single-target table names its only value field "Value". The organize
        # transform must rename whichever appears, or the header stays
        # unreadable.
        tables = [
            panel for panel in self.dashboard["panels"] if panel["type"] == "table"
        ]
        for table in tables:
            organize = next(
                t
                for t in table["transformations"]
                if t["id"] == "organize"
            )
            renamed = organize["options"]["renameByName"]
            self.assertTrue(
                any(key.startswith("Value") for key in renamed),
                "no value column rename found",
            )
            self.assertFalse(
                [
                    key
                    for key in renamed
                    if key.startswith("Value") and renamed[key] == key
                ],
                "a Value field was not renamed",
            )

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
            # Text panels (the agent query note) carry no datasource.
            datasource_uid = panel.get("datasource", {}).get("uid")
            if datasource_uid is not None:
                self.assertEqual(datasource_uid, uid)


if __name__ == "__main__":
    unittest.main()
