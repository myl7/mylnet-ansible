"""Render the cityu-gpu templates and assert the stack's wiring contracts.

Plain Jinja2 rendering, the same approach as test_clash_template.py, so the
tests run without inventories or secrets. Each assertion targets a contract
whose breakage is silent at deploy time but user-visible after: a typo'd
metric empties a panel, a mismatched datasource uid breaks every query,
rules the container never mounts kill the idle count, a copy button that
copies the wrong thing sends agents nowhere. Restating opinionated config
values (display choices, tunables) is deliberately avoided.
"""

import json
import re
import shlex
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

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
# Pinned like the playbook var: 13.3.0 cannot import datasource plugins.
GRAFANA_IMAGE = "grafana/grafana:13.2.5"

# Metric names nvitop-exporter exports (units are part of the name). Any
# other metric a query references must be a recording rule in
# prometheus-rules.yml.
EXPORTER_METRICS = {
    "gpu_utilization_Percentage",
    "gpu_memory_used_MiB",
    "gpu_memory_total_MiB",
    "gpu_memory_percent_Percentage",
    "gpu_temperature_C",
    "process_gpu_memory_MiB",
    "process_gpu_sm_utilization_Percentage",
}

# PromQL keywords and functions the dashboard's expressions use; a token
# that is neither one of these nor a metric name is a typo.
PROMQL_KEYWORDS = {"and", "by", "count", "label_join", "or", "sum", "vector"}


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


def render_compose() -> Any:
    return yaml.safe_load(
        render_template(
            "compose.yaml.j2",
            cityu_gpu_domain=DOMAIN,
            cityu_gpu_grafana_admin_password=ADMIN_PASSWORD,
            cityu_gpu_prometheus_local_port=PROM_LOCAL_PORT,
            cityu_gpu_grafana_local_port=GRAFANA_LOCAL_PORT,
            cityu_gpu_grafana_image=GRAFANA_IMAGE,
        )
    )


def load_yaml(path: Path) -> Any:
    with path.open() as f:
        data = yaml.safe_load(f)
    assert data is not None
    return data


def rule_records() -> set[str]:
    rules = load_yaml(FILES_DIR / "prometheus-rules.yml")
    return {rule["record"] for group in rules["groups"] for rule in group["rules"]}


def provisioned_datasource() -> Any:
    datasource = load_yaml(
        FILES_DIR / "grafana" / "provisioning" / "datasources" / "prometheus.yml"
    )
    return datasource["datasources"][0]


class ServiceUnitTest(unittest.TestCase):
    def test_exec_start_wiring(self) -> None:
        unit = render_template(
            "nvitop-exporter.service.j2",
            tailnet_ip="100.104.99.124",
            cityu_gpu_exporter_port=EXPORTER_PORT,
            inventory_hostname="cs11369a",
        )
        line = next(l for l in unit.splitlines() if l.startswith("ExecStart="))
        # Parse the flags instead of matching the whole string: the wiring
        # (tailnet bind, port, hostname label, user) is the contract, not the
        # flag order or the sampling interval.
        argv = shlex.split(line[len("ExecStart=") :])
        args = dict(zip(argv[1::2], argv[2::2]))
        self.assertEqual(args["--bind-address"], "100.104.99.124")
        self.assertEqual(args["--port"], str(EXPORTER_PORT))
        self.assertEqual(args["--hostname"], "cs11369a")
        self.assertIn("User=nvitop-exporter", unit)


class PrometheusConfigTest(unittest.TestCase):
    def test_targets_and_rule_files(self) -> None:
        config = yaml.safe_load(
            render_template(
                "prometheus.yml.j2",
                cityu_gpu_exporters=EXPORTERS,
                cityu_gpu_exporter_port=EXPORTER_PORT,
            )
        )
        assert config is not None
        # Every exporter-map entry becomes exactly one scrape target; the
        # order is not a contract.
        targets = [
            target
            for static in config["scrape_configs"][0]["static_configs"]
            for target in static["targets"]
        ]
        expected = {f"{ip}:{EXPORTER_PORT}" for ip in EXPORTERS.values()}
        self.assertEqual(sorted(targets), sorted(expected))
        # Rule files the config references must be files the container
        # actually mounts, or Prometheus silently runs without the rules.
        compose = render_compose()
        mounted = {
            volume.split(":")[1]
            for volume in compose["services"]["prometheus"]["volumes"]
        }
        for rule_file in config["rule_files"]:
            self.assertIn(rule_file, mounted)


class PrometheusRulesTest(unittest.TestCase):
    def test_idle_rule(self) -> None:
        rules = load_yaml(FILES_DIR / "prometheus-rules.yml")
        rule = rules["groups"][0]["rules"][0]
        expr = str(rule["expr"])
        # The idle verdict's single source: the dashboard stat and the public
        # agent query both read it. Only the shape is checked: both
        # conditions must compare exporter metrics against numeric
        # thresholds. Recomputing the verdict here would test a hand-written
        # simulator, not PromQL.
        match = re.fullmatch(r"(\w+) < (\d+) and (\w+) < (\d+)", expr)
        self.assertIsNotNone(match, f"unsupported idle-rule shape: {expr}")
        assert match is not None
        self.assertIn(match.group(1), EXPORTER_METRICS)
        self.assertIn(match.group(3), EXPORTER_METRICS)


class ComposeTest(unittest.TestCase):
    def test_stack_wiring(self) -> None:
        compose = render_compose()
        assert compose is not None
        prometheus = compose["services"]["prometheus"]
        grafana = compose["services"]["grafana"]
        # Public entry is nginx; the stack itself stays on loopback.
        self.assertEqual(prometheus["ports"], [f"127.0.0.1:{PROM_LOCAL_PORT}:9090"])
        self.assertEqual(grafana["ports"], [f"127.0.0.1:{GRAFANA_LOCAL_PORT}:3000"])
        # compose `command` replaces the image CMD, so the paths Prometheus
        # needs must all be present.
        self.assertIn("--config.file=/etc/prometheus/prometheus.yml", prometheus["command"])
        self.assertIn("--storage.tsdb.path=/prometheus", prometheus["command"])
        self.assertIn(
            "./prometheus-rules.yml:/etc/prometheus/prometheus-rules.yml:ro",
            prometheus["volumes"],
        )
        env = grafana["environment"]
        # The login page is public: sign-up and anonymous access are separate
        # switches and both must be off explicitly.
        self.assertIn("GF_USERS_ALLOW_SIGN_UP=false", env)
        self.assertIn("GF_AUTH_ANONYMOUS_ENABLED=false", env)
        # The agent-query panel's copy button uses inline JS the text panel
        # sanitizer would strip.
        self.assertIn("GF_PANELS_DISABLE_SANITIZE_HTML=true", env)
        self.assertIn(f"GF_SERVER_ROOT_URL=https://{DOMAIN}", env)
        self.assertIn(
            "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=/var/lib/grafana/dashboards/cityu-gpu.json",
            env,
        )
        # compose consumes $$ as a literal $, so passwords with $ survive.
        self.assertIn("GF_SECURITY_ADMIN_PASSWORD=pa$$$$word", env)
        # Repo policy: disposable containers restart with the host.
        self.assertEqual(prometheus["restart"], "always")
        self.assertEqual(grafana["restart"], "always")


class NginxSiteTest(unittest.TestCase):
    def test_site_wiring(self) -> None:
        site = render_template(
            "cityu-gpu.nginx.j2",
            cityu_gpu_domain=DOMAIN,
            cityu_gpu_cert=CERT,
            cityu_gpu_key=KEY,
            cityu_gpu_grafana_local_port=GRAFANA_LOCAL_PORT,
        )
        self.assertIn(f"server_name {DOMAIN};", site)
        self.assertIn(f"ssl_certificate {CERT};", site)
        # Auth lives in Grafana's own login; nginx basic auth in front of it
        # caused password-prompt storms on every auto refresh.
        self.assertNotIn("auth_basic", site)
        self.assertIn(f"proxy_pass http://127.0.0.1:{GRAFANA_LOCAL_PORT};", site)
        self.assertIn("include snippets/rproxy.conf;", site)


class DashboardTest(unittest.TestCase):
    dashboard: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        with (FILES_DIR / "grafana" / "dashboards" / "cityu-gpu.json").open() as f:
            cls.dashboard = json.load(f)

    def idle_stat_exprs(self) -> list[str]:
        # The stat expressions that count a recording rule with an empty-set
        # fallback: count() over no series returns no sample, which would
        # render as No data instead of 0.
        return [
            target["expr"]
            for panel in self.dashboard["panels"]
            if panel["type"] == "stat"
            for target in panel.get("targets", [])
            if any(
                target["expr"] == f"count({record}) or vector(0)"
                for record in rule_records()
            )
        ]

    def test_queries_reference_known_metrics(self) -> None:
        valid = EXPORTER_METRICS | rule_records()
        for panel in self.dashboard["panels"]:
            for target in panel.get("targets", []):
                expr = target["expr"]
                # Quoted strings are label names inside label_join and the
                # by/on/without clauses group by label names, not metrics;
                # every remaining token must be a known name.
                stripped = re.sub(r"\b(?:by|on|without)\s*\([^)]*\)", "", expr)
                stripped = re.sub(r'"[^"]*"', "", stripped)
                tokens = set(re.findall(r"[a-zA-Z_:][a-zA-Z0-9_:]*", stripped))
                unknown = tokens - valid - PROMQL_KEYWORDS
                self.assertEqual(unknown, set(), f"{panel['title']}: {expr}")
        self.assertEqual(
            len(self.idle_stat_exprs()),
            1,
            "no stat counts a recording rule with a vector(0) fallback",
        )

    def test_agent_query_panel_carries_the_copyable_command(self) -> None:
        text_panels = [
            panel for panel in self.dashboard["panels"] if panel["type"] == "text"
        ]
        self.assertEqual(len(text_panels), 1, "the agent-query text panel is missing")
        content = text_panels[0]["options"]["content"]
        # What the copy button actually copies is the element its onclick
        # reads, not the panel's prose around it.
        by_id = re.search(r"getElementById\('([^']+)'\)", content)
        self.assertIsNotNone(by_id, "the copy button copies no element")
        assert by_id is not None
        copied = re.search(
            rf'<[^>]*id="{by_id.group(1)}"[^>]*>(.*?)</', content, re.DOTALL
        )
        self.assertIsNotNone(copied, "the copy button's target element is missing")
        assert copied is not None
        command = next(
            line
            for line in copied.group(1).splitlines()
            if line.lstrip().startswith("curl ")
        )
        argv = shlex.split(command)
        self.assertEqual(argv[0], "curl")
        # Credentials live in ~/.netrc, never in the command: agents'
        # credential rules reject inline passwords (a Claude Desktop session
        # refused to run exactly that), so -u must not appear at all.
        self.assertIn("-n", argv)
        self.assertNotIn("-u", argv)
        copied_text = copied.group(1)
        # The real response paths: labels live under the metric object.
        self.assertIn("metric.hostname", copied_text)
        self.assertIn("metric.index", copied_text)
        url = urlsplit(next(arg for arg in argv if arg.startswith("https://")))
        self.assertEqual(url.scheme, "https")
        self.assertEqual(url.hostname, DOMAIN)
        # The proxy route must address the provisioned datasource, and the
        # query must be exactly what the idle stat shows.
        uid = provisioned_datasource()["uid"]
        self.assertEqual(url.path, f"/api/datasources/proxy/uid/{uid}/api/v1/query")
        self.assertEqual(parse_qs(url.query)["query"], self.idle_stat_exprs())

    def test_table_value_columns_are_renamed(self) -> None:
        # Multi-target tables name their value fields "Value #<refId>"; a
        # single-target table names its only value field "Value". Every value
        # column the queries produce must be renamed, or the header stays
        # unreadable.
        for table in (
            panel for panel in self.dashboard["panels"] if panel["type"] == "table"
        ):
            organize = next(
                t for t in table["transformations"] if t["id"] == "organize"
            )
            renamed = organize["options"]["renameByName"]
            ref_ids = [target["refId"] for target in table["targets"]]
            value_fields = (
                {f"Value #{ref}" for ref in ref_ids}
                if len(ref_ids) > 1
                else {"Value"}
            )
            for field in value_fields:
                self.assertIn(field, renamed, f"{table['title']}: {field}")
                self.assertNotEqual(renamed[field], field)

    def test_tables_have_a_valid_default_sort(self) -> None:
        # Without a default sort the row order is whatever Prometheus
        # returned. Which columns to sort by is a display choice; the
        # contract is that every table declares a sort and each sort key
        # names a real column, else it is a silent no-op in Grafana.
        tables = [
            panel for panel in self.dashboard["panels"] if panel["type"] == "table"
        ]
        for table in tables:
            sort_by = table["options"]["sortBy"]
            self.assertTrue(sort_by, f"{table['title']}: no default sort")
            organize = next(
                t for t in table["transformations"] if t["id"] == "organize"
            )
            columns = set(organize["options"]["renameByName"].values())
            for field in sort_by:
                self.assertIn(field["displayName"], columns)

    def test_datasource_uid_matches_provisioning(self) -> None:
        entry = provisioned_datasource()
        uid = entry["uid"]
        for panel in self.dashboard["panels"]:
            panel_uid = panel.get("datasource", {}).get("uid")
            if panel_uid is not None:
                self.assertEqual(panel_uid, uid, panel["title"])
            for target in panel.get("targets", []):
                target_uid = target.get("datasource", {}).get("uid")
                if target_uid is not None:
                    self.assertEqual(target_uid, uid, panel["title"])
        # The datasource URL must address a compose service on the port that
        # service really exposes inside the stack.
        url = urlsplit(entry["url"])
        compose = render_compose()
        self.assertIn(url.hostname, compose["services"])
        container_ports = [
            port.rsplit(":", 1)[-1]
            for port in compose["services"][url.hostname]["ports"]
        ]
        self.assertIn(str(url.port), container_ports)


if __name__ == "__main__":
    unittest.main()
