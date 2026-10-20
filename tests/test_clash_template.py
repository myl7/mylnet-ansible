"""Render the Clash subscription template and assert both flavors.

The template is rendered with plain Jinja2 instead of Ansible's templar: the
Ansible-specific ``to_json`` filter, ``move_first`` plugin filter, and
``lookup('ansible.builtin.file', ...)`` call are stubbed with matching
behavior, so the test runs without inventories or secrets. ``clash_tailnet``
selects the flavor the same way the playbook's two render tasks do.
"""

import json
import unittest
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "playbooks" / "templates" / "proxy" / "clash.yaml.j2"

PROXY_HOSTNAMES = ["bwh", "jp1", "sg1"]
PROXY_HOSTS = {"bwh": "203.0.113.1", "jp1": "203.0.113.2", "sg1": "203.0.113.3"}
PROXY_PORTS = {"bwh": 8443, "jp1": 443, "sg1": 8443}
PROXY_TAILNET_IPS = {"bwh": "100.64.0.11", "jp1": "100.64.0.12", "sg1": "100.64.0.13"}
CU_TAILNET_IP = "100.64.0.14"
CU_SOCKS_PORT = 10800
PROXY_VLESS_CREDENTIALS = {
    host: {"uuid": f"uuid-{host}", "reality_public_key": f"pub-{host}", "reality_short_id": f"sid-{host}"}
    for host in PROXY_HOSTNAMES
}
TAILNET_DOMAIN = "tailnet-d39d.ts.net"
TAILNET_HOSTNAME = "vs30-mihomo"

# The template drops the first line, matching secrets/clash_rules_extend.yaml
# whose leading `---` must not end up inside the rules list.
EXTEND_RULES = (
    "--- leading marker line\n"
    "  - DOMAIN-SUFFIX,extend.example,DIRECT\n"
    "  - DOMAIN,extend2.example,proxy"
)


def render_config(**extra_vars: Any) -> dict[str, Any]:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_PATH.parent),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    env.filters["to_json"] = lambda value: json.dumps(value)
    env.filters["move_first"] = lambda items, value: [value] + [item for item in items if item != value]
    env.globals["lookup"] = lambda plugin, path: EXTEND_RULES

    variables: dict[str, Any] = {
        "proxy_hostnames": PROXY_HOSTNAMES,
        "proxy_hosts": PROXY_HOSTS,
        "proxy_ports": PROXY_PORTS,
        "proxy_tailnet_ips": PROXY_TAILNET_IPS,
        "hostvars": {"cu": {"tailnet_ip": CU_TAILNET_IP}},
        "cityu_print_socks_port": CU_SOCKS_PORT,
        "proxy_vless_credentials": PROXY_VLESS_CREDENTIALS,
        "proxy_ai_hostname": "sg1",
        "proxy_proxy_hostname": "sg1",
        "proxy_reality_server_name": "panel.myl.moe",
        "clash_tailnet_domain": TAILNET_DOMAIN,
        "clash_tailscale_hostname": TAILNET_HOSTNAME,
        "clash_tailnet": True,
    }
    variables.update(extra_vars)
    rendered = env.get_template(TEMPLATE_PATH.name).render(**variables)
    return yaml.safe_load(rendered)


class TailscaleOutboundTest(unittest.TestCase):
    config: dict[str, Any]

    @classmethod
    def setUpClass(cls):
        cls.config = render_config()

    def test_proxy_names(self):
        self.assertEqual(
            [p["name"] for p in self.config["proxies"]],
            PROXY_HOSTNAMES
            + [f"{host}-tailnet" for host in PROXY_HOSTNAMES]
            + ["cu-tailnet", "TAILNET"],
        )

    def test_vless_proxies_unchanged(self):
        for proxy in self.config["proxies"][:3]:
            self.assertEqual(proxy["type"], "vless")
            self.assertEqual(proxy["udp"], True)
            self.assertEqual(proxy["packet-encoding"], "xudp")
            self.assertEqual(proxy["servername"], "panel.myl.moe")

    def test_tailnet_vless_variants(self):
        by_name = {p["name"]: p for p in self.config["proxies"]}
        for host in PROXY_HOSTNAMES:
            proxy = by_name[f"{host}-tailnet"]
            self.assertEqual(proxy["type"], "vless")
            self.assertEqual(proxy["server"], PROXY_TAILNET_IPS[host])
            self.assertEqual(proxy["port"], PROXY_PORTS[host])
            self.assertEqual(proxy["uuid"], PROXY_VLESS_CREDENTIALS[host]["uuid"])
            self.assertEqual(proxy["servername"], "panel.myl.moe")
            self.assertEqual(
                proxy["reality-opts"],
                {
                    "public-key": PROXY_VLESS_CREDENTIALS[host]["reality_public_key"],
                    "short-id": PROXY_VLESS_CREDENTIALS[host]["reality_short_id"],
                },
            )
            self.assertEqual(proxy["dialer-proxy"], "TAILNET")
            # The public variants stay on the public address with no dialer.
            public = by_name[host]
            self.assertEqual(public["server"], PROXY_HOSTS[host])
            self.assertNotIn("dialer-proxy", public)

    def test_cu_tailnet_node(self):
        by_name = {p["name"]: p for p in self.config["proxies"]}
        self.assertEqual(
            by_name["cu-tailnet"],
            {
                "name": "cu-tailnet",
                "type": "socks5",
                "server": CU_TAILNET_IP,
                "port": CU_SOCKS_PORT,
                "udp": False,
                "dialer-proxy": "TAILNET",
            },
        )

    def test_tailscale_outbound(self):
        tailscale = self.config["proxies"][-1]
        self.assertEqual(
            {key: tailscale[key] for key in tailscale},
            {
                "name": "TAILNET",
                "type": "tailscale",
                "hostname": TAILNET_HOSTNAME,
                "state-dir": "./tailscale",
                "dialer-proxy": "ts-underlay",
                "ephemeral": False,
                "udp": True,
                "accept-routes": True,
                "ip-version": "ipv4-prefer",
            },
        )

    def test_tailscale_auth_key_injection(self):
        config = render_config(clash_tailscale_auth_key="tskey-auth-demo")
        self.assertEqual(config["proxies"][-1]["auth-key"], "tskey-auth-demo")

    def test_proxy_groups(self):
        groups = self.config["proxy-groups"]
        self.assertEqual([g["name"] for g in groups], ["default", "proxy", "cn", "ai", "ts-underlay"])
        by_name = {g["name"]: g["proxies"] for g in groups}
        self.assertEqual(by_name["default"], ["DIRECT", "proxy"])
        self.assertEqual(
            by_name["proxy"],
            ["sg1", "bwh", "jp1", "bwh-tailnet", "jp1-tailnet", "sg1-tailnet", "cu-tailnet", "DIRECT"],
        )
        self.assertEqual(by_name["cn"], ["DIRECT", "bwh", "jp1", "sg1", "cu-tailnet"])
        self.assertEqual(by_name["ai"], ["sg1", "bwh", "jp1", "DIRECT"])
        # ts-underlay stays free of the *-tailnet nodes: they dial through
        # TAILNET, so routing the underlay over them would loop.
        self.assertEqual(by_name["ts-underlay"], ["DIRECT"] + PROXY_HOSTNAMES)

    def test_dns(self):
        dns = self.config["dns"]
        self.assertEqual(dns["nameserver-policy"], {f"+.{TAILNET_DOMAIN}": "ts://TAILNET"})
        # The global "*" filter keeps every hostname, tailnet names included,
        # on real IPs instead of fake IPs.
        self.assertIn("*", dns["fake-ip-filter"])

    def test_tailnet_rules_precede_private_rules(self):
        rules = self.config["rules"]
        self.assertEqual(
            rules[:4],
            [
                f"DOMAIN-SUFFIX,{TAILNET_DOMAIN},TAILNET",
                "IP-CIDR,100.64.0.0/10,TAILNET,no-resolve",
                "IP-CIDR,100.100.100.100/32,TAILNET,no-resolve",
                "GEOSITE,private,DIRECT",
            ],
        )
        # The broad ts.net DIRECT rule stays, behind the specific tailnet rule.
        self.assertIn("DOMAIN-SUFFIX,ts.net,DIRECT", rules)
        self.assertLess(rules.index(f"DOMAIN-SUFFIX,{TAILNET_DOMAIN},TAILNET"), rules.index("DOMAIN-SUFFIX,ts.net,DIRECT"))

    def test_existing_and_extend_rules_preserved(self):
        rules = self.config["rules"]
        for rule in (
            "GEOIP,private,DIRECT,no-resolve",
            "DOMAIN-SUFFIX,ts.net,DIRECT",
            "RULE-SET,anti-ad,REJECT",
            "DOMAIN,downloads.claude.ai,default",
            "IP-ASN,401518,ai",
            "GEOSITE,cn,cn",
            "GEOIP,CN,cn",
            "MATCH,default",
            "DOMAIN-SUFFIX,extend.example,DIRECT",
            "DOMAIN,extend2.example,proxy",
        ):
            self.assertIn(rule, rules)
        self.assertEqual(rules[-1], "MATCH,default")


class PlainFlavorTest(unittest.TestCase):
    """clash_tailnet: false — the plain subscription for clients with native
    tailscale or none: no TAILNET outbound, no tailnet nodes, groups, DNS
    policy, or rules."""

    config: dict[str, Any]

    @classmethod
    def setUpClass(cls):
        cls.config = render_config(clash_tailnet=False)

    def test_proxy_names(self):
        self.assertEqual([p["name"] for p in self.config["proxies"]], PROXY_HOSTNAMES)

    def test_no_tailnet_outbound_or_dialers(self):
        for proxy in self.config["proxies"]:
            self.assertNotIn("dialer-proxy", proxy)
        self.assertNotIn("TAILNET", [p["name"] for p in self.config["proxies"]])

    def test_proxy_groups(self):
        groups = self.config["proxy-groups"]
        self.assertEqual([g["name"] for g in groups], ["default", "proxy", "cn", "ai"])
        by_name = {g["name"]: g["proxies"] for g in groups}
        self.assertEqual(by_name["default"], ["DIRECT", "proxy"])
        self.assertEqual(by_name["proxy"], ["sg1", "bwh", "jp1", "DIRECT"])
        self.assertEqual(by_name["cn"], ["DIRECT"] + PROXY_HOSTNAMES)
        self.assertEqual(by_name["ai"], ["sg1", "bwh", "jp1", "DIRECT"])

    def test_dns(self):
        dns = self.config["dns"]
        # No ts://TAILNET policy; real-IP resolution for tailnet names stays.
        self.assertNotIn("nameserver-policy", dns)
        self.assertIn("*", dns["fake-ip-filter"])
        self.assertIn("*.ts.net", dns["fake-ip-filter"])

    def test_rules(self):
        rules = self.config["rules"]
        # The tailnet range is no longer routed to an outbound; it falls
        # through to the private rules.
        self.assertFalse(any("TAILNET" in rule for rule in rules))
        self.assertEqual(rules[0], "GEOSITE,private,DIRECT")
        self.assertIn("DOMAIN-SUFFIX,ts.net,DIRECT", rules)
        self.assertEqual(rules[-1], "MATCH,default")


if __name__ == "__main__":
    unittest.main()
