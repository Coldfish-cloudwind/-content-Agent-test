# 验证材料索引

inputs/ 是运行输入，observed/ 是本次实际运行结果。每份结果的 trace 保存步骤、模型来源、工具参数、工具返回、后续动作及简短依据；saved_plan 为已保存活动方案。

| 类别 | 文件 |
| --- | --- |
| 正常与重复 | observed/normal.json、repeat.json |
| 超预算调整 | observed/replan.json |
| 工具错误与替代 | observed/failure.json |
| 输入缺失 | observed/missing_budget.json、missing_deadline.json |
| 无可行方案 | observed/no_budget.json |
| 截止时间调整 | observed/short_time.json |
| 资料不足 | observed/no_assets.json、partial_assets.json |
| 失败续跑 | observed/interrupted.json、resumed.json |
| 外部模型实际逐步决策 | observed/live_budget.json、live_failure.json、live_stop.json |
| 可执行测试日志 | test-results.txt |
| 验证元数据 | verification.json |

离线复现命令为 python examples/reproduce.py，输出默认到 .runtime/examples/。命令和失败注入选项在 reproduce.py 的 CASES 中。mock 记录用于流程回归，live_ 记录来自本次 Codex 通过 bridge 读取实际观察后逐步选择动作，api 自动端到端尚待真实凭据验证。

用例测试时钟为 2030-05-01 09:00 +08:00，运行日期记录在 verification.json。固定时钟用于稳定复现模拟交付耗时。

新增单一版本重试证据为 observed/retry_interrupted.json 和 retry_completed.json。批量评估入口为 python -m examples.evaluate，实际三轮结果在 evaluation/，人工评分规则在 data/review-rubric.json，评分表保持待评状态。

最新十二类要求核对材料在 docs/06-requirements-audit.md。最新批量结果为 audit-evaluation/summary.json。部分成功与依赖阻断证据在 observed/partial_retry.json、partial_retained.json、partial_resumed.json、dependency_blocked.json。新外部模型路径为 observed/audited_live_budget.json 和 audited_live_error.json。
