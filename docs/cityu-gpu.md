# cityu-gpu

GPU 监控面板,覆盖两台 cityu 服务器(cs11369a 4× RTX A6000,cs659b 2× RTX PRO 5000
Blackwell)。回答两个问题:现在哪张卡空闲;不空闲的卡被谁占用。刷新 5 秒,数据采集
1 秒一次。

`playbooks/cityu-gpu.yaml` 部署,旋钮都在 playbook 的 `vars` 和
`inventories/host_vars/sg1.yaml`。依赖 nvitop-exporter(NVML + psutil,进程指标
自带 username 标签)、Prometheus、Grafana,全部开源。

## 架构

- cityu 两台各跑一个 `nvitop-exporter` systemd 服务,绑 tailnet 地址 :8000,
  `/opt/nvitop-exporter` venv 安装,系统用户 `nvitop-exporter`,60 MB 上下。
- sg1 跑 compose 栈(镜像浮动 tag,`restart: always`):
  - Prometheus `127.0.0.1:50020`,每 1s 抓两台 exporter,TSDB 留 30 天,
    数据在 `/srv/cityu-gpu/prometheus`;
  - Grafana `127.0.0.1:50019`,登录后首页即 CityU GPU dashboard,
    数据在 `/srv/cityu-gpu/grafana`。
- sg1 nginx 站点 `cityu-gpu.myl.moe`(Cloudflare proxied,泛域名 origin 证书)
  纯反代到 Grafana。认证在 Grafana 自己的登录页:共享 viewer 账号 `cityu`
  (密码存 secrets,首次建号时由 playbook 写入),会话 cookie 默认 30 天。公网只暴露这一个入口。

边界:Mylnet 与 CityU 两个管理域,sg1 通过 `host_vars/sg1.yaml` 里的字面量
`cityu_gpu_exporters`(host → tailnet IP)抓取 exporter,不做跨组 hostvars
引用,同 cityu-print 的 socks 端点先例。

## 文件

部署产物:两台的 nvitop-exporter systemd 单元(含加固 drop-in)与 `/opt`
venv、sg1 的 compose 栈与 `/srv/cityu-gpu` 数据目录、nginx 反代站点;具体
路径以 playbook 任务为准。

## 先决(手动)

- DNS:Cloudflare 里 `cityu-gpu.myl.moe` A 记录指向 sg1 公网 IP,proxied。
- secrets:`secrets.yaml` 的 `cityu_gpu_grafana_admin_password`(Grafana 管理员)
  与 `cityu_gpu_panel_password`(共享 viewer 账号 `cityu` 的登录密码,只在
  playbook 首次建号时写入)。轮换密码在 Grafana 里用 admin 改,不要重跑 playbook 期望它生效;改密的 admin API 会吊销该用户全部会话,
  所以 playbook 只在建号时调用一次。
- sg1 能达两台 exporter 的 tailnet 地址(tailnet ACL 需放行 sg1 到两台的 8000)。
  2026-10-26 时现有规则已放行 sg1 访问两台的全部端口。

## Operations

- 部署/升级:`ansible-playbook playbooks/cityu-gpu.yaml`。镜像浮动 tag,每次
  运行都会 pull;运行版本与镜像 ID 由 verify 任务打印。升级前如需回滚凭据,
  先在 sg1 `docker image inspect --format '{{.Id}}' <image>` 记下当前 ID。
- exporter 升级:pip 固定 `state: present`,升级手动执行
  `sudo /opt/nvitop-exporter/bin/pip install -U nvitop-exporter && sudo systemctl restart nvitop-exporter`。
- 改面板:改 `playbooks/files/cityu-gpu/grafana/dashboards/cityu-gpu.json` 重跑
  playbook;Grafana 的 file provider 每 10s 热加载,通常不需要重跑。想用官方
  nvitop 面板,在 Grafana UI 按 dashboard ID 22589 手动导入。
- 空闲判定阈值(显存 2048 MiB / 利用率 10)在 dashboard JSON 的 stat 查询里。

## Quirks

- 不用 nginx basic auth:面板 5 秒自动刷新时,浏览器(尤其 iOS Safari)会对
  XHR 反复弹出 basic auth 输入框,完全无法使用。Grafana 原生登录发会话
  cookie,刷新无感。
- 表格面板多查询合并后,值列的原始字段名是 `Value #A` 形态,organize
  transform 必须按这个键名改名,否则列头不可读(测试有回归断言)。

- 其中一台的 127.0.0.1:8000 已被别的服务占用;exporter 绑 tailnet 地址,
  不冲突。playbook 的端口检查只看 tailnet 地址上的绑定。
- 个别 GPU 进程的 SM 采样返回不可用(NVML 对部分进程不给计算采样),面板上
  SM 列显示空属正常;显存归因不受影响,同同学面板的「不可用」标注。
- 监控数据(含进程命令行指标)落在 sg1;公网面板要求 Grafana 登录(匿名
  访问关闭),metrics 端点只在 tailnet 内。
- 通知(空闲邮件、可订阅列表)是后续单独的工作,现在未部署。secrets 里的
  `system_smtp_*`(Mailgun)届时可直接复用。

## Manual cleanup scope

按仓库约定 playbook 只做 provisioning。若将来下线此功能,删除
`playbooks/cityu-gpu.yaml`、`playbooks/templates/cityu-gpu/`、
`playbooks/files/cityu-gpu/`、`tests/test_cityu_gpu_template.py`、本文档,以及
`host_vars/sg1.yaml` 的 `cityu_gpu_*` 段后,在各主机手动清理:

```sh
# cityu 两台
sudo systemctl disable --now nvitop-exporter
sudo rm -rf /etc/systemd/system/nvitop-exporter.service /etc/systemd/system/nvitop-exporter.service.d /opt/nvitop-exporter
sudo userdel nvitop-exporter

# sg1
docker compose --project-directory /opt/stacks/cityu-gpu down
sudo rm -rf /opt/stacks/cityu-gpu /srv/cityu-gpu
sudo rm -f /etc/nginx/sites-enabled/cityu-gpu /etc/nginx/sites-available/cityu-gpu
sudo nginx -t && sudo nginx -s reload
```
