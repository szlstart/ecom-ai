# 最终验收证据说明

本目录描述《企业级在线商城以及AI智能客服设计开发文档.md》3.34.13～3.34.19 的证据生成方式。运行时证据写入被 Git 忽略的 `artifacts/acceptance/<release>/`，避免把可能含环境信息的报告直接提交到仓库；仓库只保存脱敏模板、验证程序和证据清单。

## 当前检查点

执行 `make acceptance-audit` 生成当前差距报告。报告结论为 `no_go` 时，必须逐项修复；不得把未实现页面、缺失 API、缺失测试或外部环境证据标记为通过。

全部仓库内缺口修复后，CI 使用 `make acceptance-gate` 执行严格检查。生产等价压测、备份恢复、PITR、对象复制、镜像签名、真实模型评估和负责人签字属于环境证据，按 `docs/runbooks/release-go-no-go.md` 收集，不得用脚本存在或模拟输出替代。

`make acceptance-test` 会强制启用真实 MySQL/PostgreSQL/Redis 集成套件，并把后端覆盖率 XML 写入 `artifacts/acceptance/current/quality/`。当前仓库门禁为后端行覆盖率不低于 60%；它只用于阻止证据退化，不能替代逐流程、并发、安全、浏览器和环境验收。

`docs/test_evidence_registry.yaml` 把追踪矩阵中的测试族绑定到精确的 Pytest/Vitest 选择器，并声明证据层级。`make acceptance-gate` 会解析测试源文件，拒绝不存在或已改名的选择器、未知/孤立测试族，以及没有测试归属的领域聚合；仅在 OpenAPI 中填写 `*-*` 标签不再被视为测试证据。CI 的验收工件同时保存后端和前端 JUnit 报告。

## 证据最小结构

每个发布版本的证据目录至少包含：

- `traceability-audit.json`：页面、Operation、Permission、状态规则和测试归属检查；
- `quality/`：后端、前端、契约、并发、安全与 E2E 报告；
- `database/`：迁移、Schema Drift、备份恢复和不变量对账；
- `agent/`：固定数据集、逐用例差异、安全 Holdout、延迟和成本；
- `performance/`：Load、Stress、Spike、Soak 与容量余量；
- `operations/`：Canary、回滚、故障演练、RPO/RTO 与告警记录；
- `go-no-go.json`：每个门禁的证据 URI、Owner、结论和签字时间。

证据只允许包含公开 ID、脱敏字段和 Trace/Audit 引用，不保存 Token、Cookie、Secret、完整运单号、用户消息正文或模型隐藏推理。

从 `docs/acceptance/go-no-go.template.json` 复制发布清单，填写真实 Commit、证据引用、Owner 与复核人后执行 `make go-no-go-validate MANIFEST=artifacts/acceptance/<release>/go-no-go.json`。只有八类门禁全部为 `pass`，且每类都有证据、复核人和 UTC 决策时间，工具才返回 `go`；`pending`、`insufficient_evidence`、缺失门禁或无签字的 `pass` 均 Fail Closed 为 `no_go`。
