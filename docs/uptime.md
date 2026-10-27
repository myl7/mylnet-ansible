# uptime

uptime-kuma 部署在 sg1(`playbooks/uptime.yaml`,容器绑 sg1 回环,nginx
反代 `uptime.myl.moe`)。监控项不是部署产物:kuma 2 构建里没有管理 API,只有
socket.io + 管理员登录,所以监控项在 UI 手工维护,本文件是仓库侧清单,改动
监控时两边同步。

## 监控清单

| 名称 | 类型 | 目标 | 间隔 | 通知 |
|---|---|---|---|---|
| key.myl.moe | HTTP(s) | `https://key.myl.moe` | 60 | Uptime |
| dav.myl.moe | HTTP(s) | `https://dav.myl.moe/public/` | 60 | Uptime |
| GPU 服务器 A · SSH (22) | Port | `gpu-a.<tailnet>.ts.net:22`(FQDN,见下) | 60 | Uptime |
| GPU 服务器 A · Tailscale 连通 | Ping | `gpu-a`(裸短名) | 60 | Uptime |
| GPU 服务器 B · SSH (22) | Port | `gpu-b.<tailnet>.ts.net:22`(FQDN,见下) | 60 | Uptime |
| GPU 服务器 B · Tailscale 连通 | Ping | `gpu-b`(裸短名) | 60 | Uptime |
| cu · SOCKS (10800) | Port | `cu.<tailnet>.ts.net:10800`(FQDN,见下) | 60 | Uptime |
| cu · Tailscale 连通 | Ping | `cu` | 60 | Uptime |
| cityu-print.myl.moe | HTTP(s) | `https://cityu-print.myl.moe/`,接受状态码 401 | 60 | Uptime |
| cityu-print · 打印链路 | HTTP(s) - Keyword | `https://cityu-print.myl.moe/api/check`,关键词 `"ok":true`(紧凑无空格,见下),Basic Auth 用户 `cityu`(密码同 cityu-print 站点) | 60 | Uptime |

存量 GPU 服务器 A/B 四条监控在 kuma 里未挂通知,只有 key/dav 挂了。补挂
「Uptime」渠道,否则 cityu 侧断网只变色面板,不触发通知。

## kuma 假红(裸主机名 + Port 监控,2026-10-23 调查定论)

Port 监控目标写裸主机名(`gpu-a`)时大面积假红:GPU 服务器两条 SSH 监控自
9 月起间歇假红,10-22 18:19 UTC 起连续红 15 小时(92% 失败),每次红
的 `end_time − time` 恰好 5.000s。根因三层叠加:

1. kuma `server/monitor-types/tcp.js` 不传 timeout,落到 tcp-ping@0.1.1
   (2014 年的库)硬编码的 5s 预算,且** DNS 解析时间计入该预算**(实测
   socket 计时器在解析未完成时即触发);真实错误又被
   `catch { throw new Error("Connection failed") }` 吞掉,maxretries=0
   下一次失败立即记红。
2. Node `net.connect` 走 getaddrinfo AF_UNSPEC(A+AAAA)解析裸单标签名:
   在 sg1 解析链(容器 127.0.0.11 → 宿主 systemd-resolved)上,绝对单标签
   查询先付 ~4s 慢路径(resolved 不把单标签转发上游,LLMNR 在多条链路上
   耗尽后才 REFUSED),然后 search 展开 `gpu-a.<tailnet>.ts.net`
   才落到 MagicDNS(0ms)。慢在前半程,冷解析 3.6–7.8s,与 5s 预算赛跑。
3. 触发:2026-10-22 18:18 UTC 的 apt 升级轮(tailscale 1.102.4→1.104.2,
   docker-ce 同轮)重启 tailscaled,重新注册为 `tailscale0 default route:
   no` 并 flush 缓存,慢路径从偶发(9 月零星红)变成常态。改成 IP 后
   零红至今。

Ping 监控全程绿:实测 ping 进程墙钟 55–60ms(含解析)而同期 Node
lookup 3.6–7.8s,二者解析路径不同的内部机制未追溯(gpt-6-astra 评审
亦将此列为存疑点);公网 FQDN 的 HTTP 监控走上游解析,也一直绿。

**宿主根因与处置(2026-10-23 抓包实证;10-24 回退)**:慢的是
systemd-resolved 对绝对裸短名的处理——先在所有接口(eth0 + 全部
veth/网桥)广播 LLMNR 组播,等 2.4–3.9s 无应答才回 REFUSED(抓包
12s 窗口 519 个 5355 包);search 展开后经 tailscale0 到 MagicDNS
实际 0.4ms 应答。`LLMNR=no` 修复曾于 10-23 应用并验证有效(绝对
单标签 0ms 快速失败、容器内 Node lookup 0–10ms),10-24 按用户要求
已回退,sg1 保持发行版默认(LLMNR 开启),裸短名 ~3.7s 慢路径随之
回归——不要未经同意重新添加。常态化缓解 = port 监控目标写 FQDN
(2026-10-24 已切换,实测 48–52ms):多标签名命中 `~ts.net` routing
domain 直达 MagicDNS,不经过单标签/LLMNR 那条腿,故障期间即已验证
免疫。kuma port 监控「5s 预算含 DNS + 零重试 + 吞错误」的设计缺陷
在上游,别给它喂可能变慢的解析。

2026-10-23 部署监控时的两个补充事实。cu 的 22 端口从 sg1 不可达、从
Mac 可达(tailnet ACL 所限,已确认),所以用 SOCKS
10800 代替 SSH 22 作为 cu 的端口层监控,那也是打印链路真正的依赖。Keyword
监控对 JSON 接口要写紧凑形式 `"ok":true`:kuma 的 HTTP 客户端把响应
解析成对象再 `JSON.stringify` 后匹配,带空格的 `"ok": true` 永远不中,
而 `"ok":true` 语义精确(ok 为 false 时不匹配)。

## 覆盖设计(2026-10-23 打印事故后)

2026-10-23 15:13-16:29(HKT),校园出口到 `sg1:443` 的 WSS 隧道被中间盒劫持
(连接建立但首字节不是 TLS,从未到达 nginx)。cu 的 tailscaled 依赖这条隧道
作为唯一 DERP 通路,随之 sg1↔cu tailnet 断裂,cityu-print 的 show queue 与
submit 全部超时。网站与打印机本身全程健康,故障在传输链路上。

所以覆盖按层拆,自下而上:

- `cu · Tailscale 连通`:tailnet 断裂的第一指标,本次事故类别的告警源;
- `cu · SOCKS (10800)`:gost/mihomo/tailscaled 所在主机与服务端口的存活
  (22 从 sg1 被 tailnet ACL 挡,故用 SOCKS 端口,见下);
- `cityu-print.myl.moe`(401):前门,覆盖 Cloudflare、nginx、证书与应用;
- `cityu-print · 打印链路`(`/api/check`):全链,sg1→tailnet→cu SOCKS→
  打印机 LPD,端到端答案。

## 状态页

公开页 `https://uptime.myl.moe/status/cityu`。两个组:`cityu-print` 按链路
顺序放四条打印监控(前门、端到端、cu 隧道、cu SOCKS);`GPU 服务器(GPU 面板
底座)` 放 A/B 四条。页面描述解释四条与打印可用性的对应关系。
组的呈现归 kuma UI 管,改动后同步本文件。

## 添加监控(手动)

`uptime.myl.moe` → Add New Monitor,按上表逐条填,通知都选「Uptime」。需要
留意的两处:kuma 容器跑在 sg1 上,Ping/Port 类监控的 hostname 走 sg1 的
tailnet DNS(`cu`、GPU 服务器裸短名均可解析);`cityu-print.myl.moe` 那条要在
Advanced 里把 Accepted Status Codes 勾成 401(htpasswd 质询即健康)。
