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
            {p["name"] for p in self.config["proxies"]},
            set(PROXY_HOSTNAMES)
            | {f"{host}-tailnet" for host in PROXY_HOSTNAMES}
            | {"cu-tailnet", "TAILNET"},
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
        by_name = {p["name"]: p for p in self.config["proxies"]}
        tailscale = by_name["TAILNET"]
        self.assertEqual(tailscale["type"], "tailscale")
        # The control plane dials through the underlay selector.
        self.assertEqual(tailscale["dialer-proxy"], "ts-underlay")
        # The identity must survive subscription refreshes and restarts.
        self.assertEqual(tailscale["state-dir"], "./tailscale")

    def test_tailscale_auth_key_injection(self):
        config = render_config(clash_tailscale_auth_key="tskey-auth-demo")
        by_name = {p["name"]: p for p in config["proxies"]}
        self.assertEqual(by_name["TAILNET"]["auth-key"], "tskey-auth-demo")

    def test_proxy_groups(self):
        proxy_names = {p["name"] for p in self.config["proxies"]}
        group_names = {g["name"] for g in self.config["proxy-groups"]}
        by_name = {g["name"]: g for g in self.config["proxy-groups"]}
        # Every group member must resolve to a proxy or another group; a
        # dangling reference breaks the whole subscription.
        valid = proxy_names | group_names | {"DIRECT"}
        for group in self.config["proxy-groups"]:
            for member in group["proxies"]:
                self.assertIn(member, valid, group["name"])
        # Campus egress stays selectable from the cn group.
        self.assertIn("cu-tailnet", by_name["cn"]["proxies"])
        # The underlay selector must never route through TAILNET itself or
        # the *-tailnet nodes: they dial through TAILNET and would loop.
        underlay = by_name["ts-underlay"]["proxies"]
        self.assertNotIn("TAILNET", underlay)
        for member in underlay:
            self.assertFalse(member.endswith("-tailnet"), member)

    def test_dns(self):
        dns = self.config["dns"]
        self.assertEqual(dns["nameserver-policy"], {f"+.{TAILNET_DOMAIN}": "ts://TAILNET"})
        # The global "*" filter keeps every hostname, tailnet names included,
        # on real IPs instead of fake IPs.
        self.assertIn("*", dns["fake-ip-filter"])

    def test_sniffer(self):
        sniffer = self.config["sniffer"]
        self.assertTrue(sniffer["enable"])
        # Outbounds dial the sniffed domain, never the client-resolved IP:
        # behind a poisoned resolver that IP is garbage.
        self.assertTrue(sniffer["override-destination"])

    def test_tailnet_rules_precede_private_rules(self):
        rules = self.config["rules"]
        tailnet_rules = [
            rule
            for rule in rules
            if rule.endswith((",TAILNET", ",TAILNET,no-resolve"))
        ]
        self.assertTrue(tailnet_rules, "no rule routes to TAILNET")
        first_private = min(
            rules.index(rule)
            for rule in ("GEOSITE,private,DIRECT", "GEOIP,private,DIRECT,no-resolve")
            if rule in rules
        )
        for rule in tailnet_rules:
            self.assertLess(rules.index(rule), first_private, rule)
        # The broad ts.net DIRECT rule stays, behind the specific tailnet rule.
        self.assertIn("DOMAIN-SUFFIX,ts.net,DIRECT", rules)
        self.assertLess(
            rules.index(f"DOMAIN-SUFFIX,{TAILNET_DOMAIN},TAILNET"),
            rules.index("DOMAIN-SUFFIX,ts.net,DIRECT"),
        )

    def test_existing_and_extend_rules_preserved(self):
        rules = self.config["rules"]
        # The extend file's rules land in the rules list; its leading `---`
        # marker must not (it would break rule parsing).
        self.assertIn("DOMAIN-SUFFIX,extend.example,DIRECT", rules)
        self.assertIn("DOMAIN,extend2.example,proxy", rules)
        self.assertIn("GEOIP,private,DIRECT,no-resolve", rules)
        self.assertIn("DOMAIN-SUFFIX,ts.net,DIRECT", rules)
        self.assertEqual(rules[-1], "MATCH,default")

    def test_ai_rules(self):
        rules = self.config["rules"]
        ai_rules = [rule for rule in rules if rule.rsplit(",", 1)[-1] == "ai"]
        domain_ai = [rule for rule in ai_rules if rule.startswith("DOMAIN")]
        asn_ai = [rule for rule in ai_rules if rule.startswith("IP-ASN")]
        # Both routing dimensions exist: domain-based and ASN-based.
        self.assertTrue(domain_ai)
        self.assertTrue(asn_ai)
        # Every AI rule stays ahead of the final match. The domain rules
        # also precede the first CN capture; the ASN catch-alls deliberately
        # sit between GEOSITE,cn and GEOIP,CN (domain match is the precise
        # signal, ASN ranges only catch leftover egress IPs).
        for ai_rule in ai_rules:
            self.assertLess(rules.index(ai_rule), rules.index("MATCH,default"))
        for ai_rule in domain_ai:
            self.assertLess(rules.index(ai_rule), rules.index("GEOSITE,cn,cn"))
        for ai_rule in asn_ai:
            self.assertLess(rules.index(ai_rule), rules.index("GEOIP,CN,cn"))


class PlainFlavorTest(unittest.TestCase):
    """clash_tailnet: false — the plain subscription for clients with native
    tailscale or none: no TAILNET outbound, no tailnet nodes, groups, DNS
    policy, or rules."""

    config: dict[str, Any]

    @classmethod
    def setUpClass(cls):
        cls.config = render_config(clash_tailnet=False)

    def test_proxy_names(self):
        self.assertEqual(
            {p["name"] for p in self.config["proxies"]}, set(PROXY_HOSTNAMES)
        )

    def test_no_tailnet_outbound_or_dialers(self):
        for proxy in self.config["proxies"]:
            self.assertNotIn("dialer-proxy", proxy)
        self.assertNotIn("TAILNET", [p["name"] for p in self.config["proxies"]])

    def test_proxy_groups(self):
        proxy_names = {p["name"] for p in self.config["proxies"]}
        group_names = {g["name"] for g in self.config["proxy-groups"]}
        # Flavor isolation: the plain subscription has no underlay selector.
        self.assertNotIn("ts-underlay", group_names)
        valid = proxy_names | group_names | {"DIRECT"}
        for group in self.config["proxy-groups"]:
            for member in group["proxies"]:
                self.assertIn(member, valid, group["name"])

    def test_dns(self):
        dns = self.config["dns"]
        # No ts://TAILNET policy; real-IP resolution for tailnet names stays.
        self.assertNotIn("nameserver-policy", dns)
        self.assertIn("*", dns["fake-ip-filter"])
        self.assertIn("*.ts.net", dns["fake-ip-filter"])

    def test_sniffer(self):
        sniffer = self.config["sniffer"]
        self.assertTrue(sniffer["enable"])
        # Outbounds dial the sniffed domain, never the client-resolved IP:
        # behind a poisoned resolver that IP is garbage.
        self.assertTrue(sniffer["override-destination"])

    def test_rules(self):
        rules = self.config["rules"]
        # The tailnet range is no longer routed to an outbound; it falls
        # through to the private rules.
        self.assertFalse(any("TAILNET" in rule for rule in rules))
        self.assertIn("GEOSITE,private,DIRECT", rules)
        self.assertIn("DOMAIN-SUFFIX,ts.net,DIRECT", rules)
        self.assertEqual(rules[-1], "MATCH,default")


if __name__ == "__main__":
    unittest.main()
