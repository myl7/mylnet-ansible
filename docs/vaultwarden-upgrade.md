# Vaultwarden latest-alpine 升级与回滚

本文件是待批准的部署操作单。此次只修改本地仓库、执行本地检查和线上只读核实；下列拉取、备份、停机、部署及回滚命令均未执行。不要跳过一致性备份直接运行 playbook。

## 已核实的状态（2026-10-07 UTC）

- 当时核实的官方最新稳定版：1.37.4，发布于 2026-10-05，包含组织成员撤销等安全修复；这是历史快照，不是固定部署目标。
- 目标：`sg1` / `key.myl.moe`，SSH `myl@167.104.101.86:50000`。
- 线上二进制与 `/api/version` 均为 **1.37.3**，容器健康。
- 旧镜像：`vaultwarden/server:latest-alpine`；只读检查时的 image ID 和 RepoDigest 均指向 `sha256:9a905c5cf5df61bb827da3805e160170741c0b53b0c63bb07e1afd3d2e21b1c8`。部署前重新记录，不依赖此快照。
- 挂载：`/srv/vaultwarden/data:/data`；默认 SQLite，存在 `db.sqlite3`，未设置自定义 `DATABASE_URL`；`config.json` 不存在。
- 宿主架构：`x86_64`；Docker Compose `v5.5.1`。
- 部署使用 `latest-alpine`，每次执行 playbook 都拉取镜像，去除重复容器重建，等待健康并记录实际版本、核对镜像 ID 和检查数据库连接。端口、令牌、注册策略和可信代理设置保持原样。

## 镜像更新策略

按仓库偏好使用 `vaultwarden/server:latest-alpine`，避免为每次上游发布修改版本并提交仓库。更新由执行 playbook 触发；标签本身不会自动更新正在运行的容器，也没有新增自动更新服务。部署前核对实际目标版本的官方升级说明，确认数据库和代理兼容性。以下 1.37.4 的升级说明保留作为历史依据，不能代替以后版本的发布说明。

## 代理链及升级要求

已只读核对线上与仓库的相关指令一致：Nginx 只信任指定 Cloudflare 网段，通过 `CF-Connecting-IP` 得到 `$remote_addr`，再覆盖 `X-Real-IP`。线上没有设置 `IP_HEADER` / `IP_HEADER_TRUSTED_PROXIES`，也没有管理界面配置覆盖；容器网关为 `172.27.0.1`。

1.37.4 的默认值仍是 `IP_HEADER=X-Real-IP` 和 `IP_HEADER_TRUSTED_PROXIES=local`。`local` 包含非公网地址，当前本机 Nginx → Docker 私网网桥链路无需新增信任网段。本次线上 `nginx -t` 已通过，但有原有的 HTTP/2 指令弃用和其他站点 protocol options 警告；此次不扩大修复范围。部署前重新核对相关指令，只检查此站点和 real-ip 指令，不打印完整配置或环境变量。

如果之后改用 `X-Forwarded-For`，1.37.4 会从右向左选取第一个非可信地址，需要按真实拓扑配置 `IP_HEADER_TRUSTED_PROXIES`。不要设为 `all`，也不要猜测 Docker 网关或把所有私网都加入显式列表。不要在已有 `include snippets/rproxy.conf` 后重复添加同名 `proxy_set_header`。

1.37.3 → 1.37.4 未列出必须手工执行的迁移或中间版本，但程序启动会自动迁移数据库，因此回滚必须恢复升级前数据。当前使用 SQLite，不适用发布说明中的 Alpine/MariaDB TLS 注意项；不要添加关闭 TLS 验证的变量。若使用较旧 Bitwarden CLI，部署后应验证 Send 接收功能。

## 批准后的部署步骤

先约定维护窗口：完整数据冷备需要短暂停止 Vaultwarden。通知使用者避免写入；回滚会丢失备份之后产生的写入。下列命令分为 **sg1 上的 root shell** 和 **Mac 本地** 两部分，不要混用。

### 1. sg1：预拉取、记录旧版本并冷备

通过现有 SSH 连接进入 sg1 后，在 root Bash 中执行。以下步骤默认仍使用已核实的 SQLite；若部署前数据库类型、挂载或代理拓扑发生变化，先更新方案。

```bash
sudo -i
bash
set -euo pipefail
umask 077
stack_dir=/opt/stacks/vaultwarden
backup_dir="/srv/backups/vaultwarden/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_dir"
cd "$stack_dir"

docker compose config --quiet
container_id=$(docker compose ps -q vaultwarden)
test -n "$container_id"
docker exec "$container_id" /vaultwarden --version > "$backup_dir/version.txt"
docker inspect --format '{{.Image}}' "$container_id" > "$backup_dir/old-image-id.txt"
docker image inspect --format '{{json .RepoDigests}}' \
  "$(cat "$backup_dir/old-image-id.txt")" > "$backup_dir/old-image-digests.txt"

# 在服务仍运行时完成下载及镜像保存；失败则不要停机。
docker pull vaultwarden/server:latest-alpine
test "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' \
  vaultwarden/server:latest-alpine)" = linux/amd64
docker image inspect --format '{{json .RepoDigests}}' \
  vaultwarden/server:latest-alpine > "$backup_dir/new-image-digests.txt"
docker image save --output "$backup_dir/old-image.tar" \
  "$(cat "$backup_dir/old-image-id.txt")"

# 包含现有 Compose/.env 等配置；这些备份不可贴到日志或聊天。
tar -C "$stack_dir" -cpf "$backup_dir/stack.tar" .
cp -a /etc/nginx/sites-available/vaultwarden "$backup_dir/nginx-site"
df -h /srv/vaultwarden /srv/backups/vaultwarden
du -sh /srv/vaultwarden/data

# 确认空间足够容纳完整数据备份后，才执行下面三行。
docker compose stop vaultwarden
tar -C /srv/vaultwarden -cpf "$backup_dir/data.tar" data
(cd "$backup_dir" && sha256sum data.tar stack.tar old-image.tar nginx-site > SHA256SUMS)
(cd "$backup_dir" && sha256sum --check SHA256SUMS)
printf 'Backup directory: %s\n' "$backup_dir"
```

记录输出的备份目录；备份留在 sg1，不上传。完整数据归档保留 SQLite、对应 WAL、附件、Sends、密钥及配置，不读取其中条目。校验和只能检测文件变化，不代表已完成恢复演练；条件允许时在隔离环境演练恢复。

如果停机后的备份或校验失败，**不要执行升级**。此时原 Compose 和数据未更改，可在原目录执行 `docker compose start vaultwarden` 恢复原服务，再处理备份问题。停机后不要在备份与部署之间重新开放写入。

### 2. Mac：运行已审核的 playbook

确认上一步完整成功且记录了备份目录后，在 Mac 的仓库中运行：

```bash
cd ~/app/mylnet-ansible
ansible-playbook playbooks/vaultwarden.yaml --syntax-check
ansible-playbook playbooks/vaultwarden.yaml --limit sg1
```

不要加 `--diff`、`-v` 或打印 `docker compose config`，以免显示管理员令牌。正常执行会重新拉取 `latest-alpine`，Compose 自行按需重建一次，最长等待健康 120 秒，应用 Nginx handler，然后读取本机 `/api/version`、核对运行镜像 ID 与本次拉取标签的镜像 ID 一致，输出实际版本和镜像 ID，并检查 `/api/alive == 200`。浮动标签可能在预拉取与 playbook 执行之间变化，以 playbook 完成时记录的实际版本和镜像 ID 为准；部署前的镜像摘要仅是预拉取快照。失败时保留备份，按失败阶段诊断或执行下面的回滚；playbook 不会自动恢复数据库。

### 3. 部署验证

在 sg1 检查，只输出版本和健康字段：

```bash
cd /opt/stacks/vaultwarden
container_id=$(docker compose ps -q vaultwarden)
docker exec "$container_id" /vaultwarden --version
docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id"
sudo nginx -t
curl --fail --silent --show-error --noproxy '*' http://127.0.0.1:50002/api/version
curl --fail --silent --show-error --noproxy '*' http://127.0.0.1:50002/api/alive
```

在 Mac 检查公网 HTTPS（验证证书，不使用 `-k`）：

```bash
curl --fail --silent --show-error --max-time 15 https://key.myl.moe/api/version
curl --fail --silent --show-error --max-time 15 https://key.myl.moe/api/alive
```

本地与公网版本应与 playbook 记录的实际版本一致，健康应为 `healthy`，两个 alive 接口应为 HTTP 200。`/api/alive` 会获取数据库连接；`/api/config.version` 是协议兼容版本，不能用来判断 Vaultwarden 安装版本。使用者自行测试登录、同步、附件、通知/WebSocket 和组织权限撤销；此次不代用户读取密码库或修改组织成员。若检查客户端 IP，核对实际 Nginx 覆盖的 `X-Real-IP`，不应采纳客户端伪造的 XFF。

## 失败后的回滚（需要维护窗口）

在 sg1 的 root Bash 中，将 `backup_dir` 替换成上面记录的真实路径。以下恢复会回到备份时点；新版本运行后产生的写入只保留在隔离目录中，需另行处理。

```bash
set -euo pipefail
umask 077
backup_dir=/srv/backups/vaultwarden/REPLACE_WITH_RECORDED_DIRECTORY
test -f "$backup_dir/SHA256SUMS"
(cd "$backup_dir" && sha256sum --check SHA256SUMS)
stack_dir=/opt/stacks/vaultwarden
cd "$stack_dir"
docker compose stop vaultwarden

# 保留升级后的数据，不将新旧数据库/WAL 混合。
failed_dir="/srv/vaultwarden/data.after-upgrade.$(date -u +%Y%m%dT%H%M%SZ)"
test ! -e "$failed_dir"
mv /srv/vaultwarden/data "$failed_dir"
tar -C /srv/vaultwarden -xpf "$backup_dir/data.tar"
tar -C "$stack_dir" -xpf "$backup_dir/stack.tar"
cp -a "$backup_dir/nginx-site" /etc/nginx/sites-available/vaultwarden

docker image load --input "$backup_dir/old-image.tar"
docker tag "$(cat "$backup_dir/old-image-id.txt")" vaultwarden-rollback:restore
printf 'services:\n  vaultwarden:\n    image: vaultwarden-rollback:restore\n' \
  > "$backup_dir/rollback-image.yaml"
docker compose -f compose.yaml -f "$backup_dir/rollback-image.yaml" \
  up -d --pull never --wait --wait-timeout 120 vaultwarden
nginx -t
nginx -s reload
```

随后重复本地/公网版本及健康验证，版本应与备份中的 `version.txt` 一致。回滚时用覆盖文件指定保留下来的旧镜像，不重新拉取会漂移的 `latest-alpine`。保留并记录此覆盖文件的使用方式；在升级问题解决前，不要重新运行会拉取 `latest-alpine` 的 playbook。旧版本仍有安全漏洞，回滚只是恢复可用性的临时措施。

## 初次修复验证记录（2026-10-07，固定版本方案）

本次使用不含真实 secrets 的临时副本和占位令牌进行检查：

- Ansible 语法检查、主机列表和任务列表通过，目标仅 `sg1`。
- `ansible-lint --offline`：0 errors / 0 warnings，达到 production profile；Prettier 检查通过。
- 占位 Compose 经 sg1 现有 Compose 的 `config` 只读解析通过；确认固定镜像、令牌美元符转义、回环绑定及数据卷不变。Mac 未安装 Compose 插件，因此没有本机容器启动测试。
- 本机隔离 HTTP fixture 执行实际健康检查任务：正确版本与健康数据库通过，旧版本和数据库 503 均失败，check mode 不发出 HTTP 请求。
- 现有仓库 15 个 Python 单元测试通过（使用 Ansible 的 Python 3.14；系统 Python 3.9 不支持现有测试依赖的 `typing.Never`）。
- 独立审查通过；5 个操作单 Bash 代码块通过 `bash -n`。

尚未进行实际线上升级、备份恢复演练或升级后的客户端验收；这些需在批准后的维护窗口完成。

## 浮动标签方案验证记录（2026-10-08）

- Ansible 语法、lint 和格式检查通过。
- `latest-alpine` 模板渲染通过，令牌转义、回环端口和数据卷不变。
- 本机隔离 HTTP 服务和 Docker 命令替身验证通过：当前版本和未来版本均可通过，非法版本响应、运行镜像不一致、数据库故障会失败；check mode 不执行 HTTP 或 Docker 检查。
- 操作单中的 5 个 Bash 代码块通过 `bash -n`。

此轮验证没有连接或部署线上服务；Docker 命令验证使用隔离替身，没有启动真实容器。

## 依据

- [1.37.4 官方发布说明](https://github.com/dani-garcia/vaultwarden/releases/tag/1.37.4)
- [1.37.4 配置默认值](https://github.com/dani-garcia/vaultwarden/blob/1.37.4/src/config.rs)
- [1.37.4 代理 IP 解析](https://github.com/dani-garcia/vaultwarden/blob/1.37.4/src/auth.rs)
- [版本与数据库健康接口](https://github.com/dani-garcia/vaultwarden/blob/1.37.4/src/api/core/mod.rs)
- [启动时的数据库迁移](https://github.com/dani-garcia/vaultwarden/blob/1.37.4/src/db/mod.rs)
- [官方备份说明](https://github.com/dani-garcia/vaultwarden/wiki/Backing-up-your-vault)
