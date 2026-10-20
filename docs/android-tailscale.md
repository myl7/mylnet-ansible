# Android tailnet access through the FlClash subscription

`playbooks/proxy.yaml` (the `proxy-sub` play) embeds a `tailscale` outbound in
the Clash subscription, so FlClash reaches the tailnet through its single VPN
alongside the regular proxy rules. No per-device override scripts are needed:
refreshing the subscription delivers everything.

## Two subscription flavors

The template renders twice, selected by the `clash_tailnet` variable:

- Plain — the default URL (`proxy_sub_id` in `playbooks/secrets/secrets.yaml`):
  the public proxy nodes and rules only, for clients with native tailscale
  (the Mac's Clash Party) or no tailnet at all. The tailnet range falls
  through to the private-range DIRECT rules.
- Tailnet — the same id plus a `/tailnet` path component
  (`https://<proxy_sub_domain>/<proxy_sub_id>/tailnet`): adds everything in
  this document (TAILNET outbound, tailnet nodes, DNS policy, rules). This
  is the FlClash-class subscription for the phone. The proxy-sub-worker
  merges this flavor, so the worker subscription carries the tailnet parts
  too.

## What the subscription adds

- `TAILNET` proxy: an embedded tsnet client. It is non-ephemeral, enables UDP,
  accepts advertised subnet routes, and stores its identity in
  `state-dir: ./tailscale` inside FlClash's private storage.
- `ts-underlay` select group: the underlay selector for the `TAILNET`
  outbound (control plane, DERP, WireGuard), currently dormant — the
  subscription's TAILNET outbound carries no `dialer-proxy` until a FlClash
  release ships a core with the wildcard proxydialer fix for
  [mihomo#3142](https://github.com/MetaCubeX/mihomo/issues/3142) (the fix is
  in FlClash's core as of 2026-10-12 but ahead of the v0.8.99 release;
  cores without it drop all tailnet traffic once `dialer-proxy` is set).
  When it ships, add `dialer-proxy: ts-underlay` to the TAILNET outbound.
- Tailnet proxy nodes: `<host>-tailnet` for every proxy host (bwh, sg1,
  jp1) plus `cu-tailnet`. Each is the machine's proxy service dialed at its
  tailnet IP through the TAILNET outbound (`dialer-proxy: TAILNET` on the
  node, a plain TCP chain that works on stock cores), so it is usable
  wherever the tailnet is reachable, also when the public endpoint is not.
  `cu-tailnet` is cu's SOCKS at the cityu-print transport port and egresses
  on the CityU campus network. The `proxy` group lists them all; `cn` lists
  `cu-tailnet`. They must never join `ts-underlay` (they dial through
  TAILNET and would loop).
- DNS: `nameserver-policy` resolves `*.tailnet-d39d.ts.net` via `ts://TAILNET`;
  every other domain keeps the existing nameservers. Bare short names
  (`http://sg1:50015`) are NOT supported: neither mihomo (no search-domains
  option) nor the tailscale outbound expands DNS search domains, so use the
  full `*.tailnet-d39d.ts.net` names.
- Rules: the tailnet domain, `100.64.0.0/10` (tailnet range), and
  `100.100.100.100/32` (MagicDNS resolver) route to `TAILNET` ahead of the
  private-range DIRECT rules that would otherwise capture them.

## Requirements

- FlClash >= 0.8.93 (bundled mihomo core has tailscale support; 0.8.99 is
  current as of 2026-10). The `<host>-tailnet` nodes work on these stock
  cores: their `dialer-proxy: TAILNET` is a TCP-only chain.
- A core with the wildcard proxydialer fix (mihomo#3142) is additionally
  needed only for the dormant ts-underlay underlay switching; the
  subscription deliberately ships without it until a FlClash release
  carries the fixed core.

## First login

`TAILNET` starts on demand: tsnet only logs in once traffic actually hits a
tailnet rule, so an empty Tailscale admin console is not an error by itself.
(Cores that start tsnet eagerly bring it up together with the VPN instead.)

The login is driven by a one-time auth key added on the device, inside FlClash:

1. Generate an auth key in the Tailscale admin console (single use, not
   ephemeral, no tags).
2. In FlClash, open the subscription profile's editor. In the `TAILNET`
   proxy, right below `hostname: vs30-mihomo`, add a line with the same
   indentation:

       auth-key: "tskey-auth-..."

3. Save, keep the VPN running, and open `http://100.69.54.3/` (or any
   tailnet-d39d.ts.net name) in a browser. The page does not need to load;
   the attempt alone triggers the on-demand start and the key login.
4. The node `vs30-mihomo` appears in the admin console. Refresh the
   subscription once afterwards: the local edit (and the key) disappears,
   while the identity stays in `state-dir`.

The identity persists in `state-dir`. Subscription refreshes, FlClash
restarts, and FlClash updates keep it; only clearing FlClash app data logs the
device out. Without an auth key the fallback is the interactive flow: set the
in-app log level to info, open a tailnet name, and tap the login URL that
appears in the logs.

As an operator-side alternative for future devices, setting
`clash_tailscale_auth_key` in `playbooks/secrets/secrets.yaml` and re-running
`ansible-playbook playbooks/proxy.yaml --tags proxy-sub` bakes the key into
the served subscription until the next deploy without it.

## MagicDNS requires one FlClash override tweak

FlClash's DNS override must stay enabled: its `listen`/hijack wiring is what
carries the VPN's DNS queries into mihomo, and disabling it breaks all DNS on
the device. But the override must not swallow the subscription's tailnet
policy. FlClash merges the override per key, so remove the one key we need
from the override set:

1. In FlClash, open the DNS override page (覆写 → DNS) and its edit action,
   which shows a YAML document listing only the overridden keys.
2. Delete the `nameserver-policy:` block and save. From then on that key
   flows through from the subscription, so
   `"+.tailnet-d39d.ts.net": ts://TAILNET` reaches the core while the rest
   of the DNS override keeps working.
3. Reconnect the VPN and open `http://sg1.tailnet-d39d.ts.net/`. Any HTTP
   response — even a refused connection — means resolution worked; the log
   should show the request matching `DomainSuffix(tailnet-d39d.ts.net)` with
   outbound `TAILNET`.

Keep the override document free of `nameserver-policy` after future FlClash
reinstalls; the policy itself comes from the subscription and needs no
per-device maintenance.

## Acceptance checklist

Run with log level info; each connection line records the matched rule and
outbound, e.g. `... --> TAILNET[domain]` or `... --> DIRECT`.

| Test | Expected |
|---|---|
| Ordinary CN website | `DIRECT` via `cn`, unchanged |
| Ordinary foreign website | `proxy` group, unchanged |
| AI website | `ai` group, unchanged |
| `http://100.69.54.3/` (cu by tailnet IP) | loads, outbound `TAILNET` |
| `http://cu.tailnet-d39d.ts.net/` | loads, outbound `TAILNET` |
| UDP echo to cu (below) | echoed, outbound `TAILNET`, `[UDP]` in the log |
| Subscription refresh | tailnet still reachable, same device identity |
| FlClash restart | tailnet still reachable, no re-login |
| Wi-Fi <-> mobile data switch | tailnet recovers within ~a minute |
| Tailscale unavailable | ordinary proxy traffic still works |
| Select `sg1-tailnet` in `proxy` | traffic flows, log shows the vless over `TAILNET` |
| Select `cu-tailnet` in `cn` | campus sites load, egress IP is CityU's |
| Switch ts-underlay to a VLESS node | only after a FlClash release carries the fixed core |

For the UDP row, an echo server on `cu` port 35353/udp (started for the
acceptance run, auto-expires after 24h):

```sh
ssh myl@100.69.54.3
nohup timeout 24h python3 -c 'import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", 35353))
while True:
    data, addr = s.recvfrom(65535)
    s.sendto(data, addr)' >/tmp/tailnet-udp-echo.log 2>&1 &
```

From the phone, e.g. with Termux: `pkg install nmap-ncat`, then
`ncat -u 100.69.54.3 35353`, type a line, and expect it echoed back.

## Stability checklist (WP2)

After the acceptance list passes, confirm the identity survives the lifecycle
events: FlClash backgrounded, screen locked, VPN torn down by the system,
phone rebooted, FlClash app updated. After each, tailnet must become reachable
again without a new login. If recovery is slow, enable Always-On VPN for
FlClash and exclude it from battery optimization.

## Troubleshooting

- No device in the admin console: open a tailnet address first; the outbound
  connects lazily.
- DNS behaves oddly: disable Android Private DNS during the test and retry.
