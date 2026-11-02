# docs

本仓库本身公开(inventories/hosts.toml 等已含全部主机名与地址)。文档只写
playbook 表达不了的内容:方法、经验、根因分析和手工操作单;文件名与对应
playbook 一致(vaultwarden.md ↔ playbooks/vaultwarden.yaml、proxy.md ↔
playbooks/proxy.yaml)。

与仓库已公开事实一致的主机名、域名、地址、端口、账号名可以直接写,不要
另造占位名。需要脱敏的只有两类:cs11369a/cs659b 两台机器上超出仓库披露的
内部状态(既有配置缺陷、故障历史等,用 server A/B 占位),以及凭据及其
相互关系(如两个服务共用密码)。tests/test_docs_public_safe.py 对仓库未
暴露的标识符 fail-closed。
