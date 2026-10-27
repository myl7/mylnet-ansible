# docs

本目录的文档会发布到公开仓库,只写 playbook 表达不了的内容:方法、经验、
根因分析和手工操作单。机器事实(主机名、IP、端口、账号名)属于 playbooks
和 inventories,不写进文档;需要时用角色或占位符表述(如 server A、
`<tailnet>.ts.net`、`<cu-ip>`)。

tests/test_docs_public_safe.py 对已知标识符 fail-closed,新增或修改文档后
先跑 `make test`。
