# docs

本仓库本身公开(inventories/hosts.toml 等已含全部主机名与地址)。文档只写
playbook 表达不了的内容:方法、经验、根因分析和手工操作单;文件名与对应
playbook 一致(vaultwarden.md ↔ playbooks/vaultwarden.yaml、proxy.md ↔
playbooks/proxy.yaml)。

与仓库已公开事实一致的主机名、域名、地址、端口、账号名可以直接写,不要
另造占位名。不写的只有两类:cs11369a/cs659b 两台机器上超出仓库披露的内部
状态(既有配置缺陷、故障历史——直接不写,不做可反查的占位映射),以及
凭据及其相互关系(如两个服务共用密码)。不要在仓库里列出被禁的具体词,
列出来本身就是泄露。
