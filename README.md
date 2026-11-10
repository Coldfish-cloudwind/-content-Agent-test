# 内容任务规划 Agent

面向小影校招 AI Agent 开发岗位的本地项目。运营人员输入主题、预算和截止时间，Agent 通过素材检索、询价、脚本制作和方案保存完成活动策划。支持超预算调整、工具失败替代、部分素材、重复执行和断点续跑。

## 评审入口

先阅读 [需求](docs/01-requirements.md)、[设计](docs/02-solution.md) 和 [验证结果](docs/03-evaluation.md)。实际输入在 examples/inputs/，实际输出与完整 Trace 在 examples/observed/。AI 使用范围与贡献归属见 [AI 使用记录](docs/04-ai-usage.md)。

- Python 3.10+，仅使用标准库，无第三方框架和安装依赖。
- 验证环境为 Windows、Python 3.13.12。全部命令在项目根目录执行。
- 金额为整数模拟单位。数据均为合成素材描述。
- 默认 mock 是固定规则测试控制器，只验证工程流程。
- api 是模型自动决策入口，每轮向模型提供工具观察，接收 JSON 动作并执行。
- bridge 允许外部模型逐轮接收观察、提交动作。本次 Codex 实际通过该入口跑完两条调整路径及一条停止路径。它需要外部控制器，自动 API 链路尚待接入真实凭据验收。

## 快速运行

```powershell
python --version
python -m pip install -r requirements.txt
python -m src.content_agent examples/inputs/replan.json --as-of 2030-05-01T09:00:00+08:00 --out .runtime/result.json
python -m unittest discover -s tests -v
python examples/reproduce.py
```

最后一条命令会在 .runtime/examples/ 生成 18 份离线输出，使用独立临时数据库。单次运行正常结果的 total_cost 为 8，saved_plan 包含脚本、素材引用、费用和制作时序。输出 JSON 中 trace 包含动作参数、工具返回、简要依据及下一动作。

`--as-of` 是明确的测试时钟。真实任务请移除此参数，并在输入中填写未来截止时间及新的 campaign_id。所有输入时间必须包含时区。

## 自动模型模式

采用 Chat Completions 风格的模型接入。模型每轮返回 action、args、reason 三字段 JSON。兼容端点的具体模型支持情况需在目标提供商验证。

```powershell
$env:PLANNER_MODEL = "你的模型名称"
$env:PLANNER_BASE_URL = "https://api.openai.com/v1"
$env:PLANNER_API_KEY = "在本机设置的密钥"
python -m src.content_agent examples/inputs/replan.json --model api --db .runtime/api.sqlite --as-of 2030-05-01T09:00:00+08:00 --out .runtime/api-result.json
```

变量从进程环境读取，.env.example 用于说明配置。真实密钥请保留在环境中。默认端点需要自行配置有效模型与凭据。模型请求超时为 30 秒，响应损坏、端点错误或配置缺失返回 incomplete/model_error。支持 HTTPS 及本机 HTTP 回环端点。

每次决策都重新传入真实工具观察，执行层核查依赖、预算和截止时间。默认最多 10 步，含停止和错误决策，所有续跑累计最多 32 步。

## 外部模型逐步执行

```powershell
python -m src.content_agent examples/inputs/live_budget.json --model bridge --db .runtime/bridge.sqlite --as-of 2030-05-01T09:00:00+08:00 --out .runtime/observation.json
```

把输出中的 model_request 交给模型，将它返回的动作与当前 campaign_id、revision 保存为 .runtime/decision.json。示例如下。

```json
{
  "campaign_id": "live_budget",
  "revision": 0,
  "decision": {
    "action": "search_assets",
    "args": {},
    "reason": "先确认可用素材。"
  }
}
```

```powershell
python -m src.content_agent examples/inputs/live_budget.json --model bridge --db .runtime/bridge.sqlite --as-of 2030-05-01T09:00:00+08:00 --decision-file .runtime/decision.json --external-name your-model --out .runtime/observation.json
```

每次仅执行一个模型动作，并返回新观察。按新的 revision 继续直到 completed 或 incomplete。过期响应被拒绝，外部名称为控制器自报字段。真实逐步记录见 examples/observed/live_budget.json 和 live_failure.json。

## 失败、重复执行和续跑

```powershell
python -m src.content_agent examples/inputs/resume.json --db .runtime/resume.sqlite --as-of 2030-05-01T09:00:00+08:00 --fail-once save_plan --max-steps 4 --out .runtime/interrupted.json
python -m src.content_agent examples/inputs/resume.json --db .runtime/resume.sqlite --as-of 2030-05-01T09:00:00+08:00 --resume --fail-once save_plan --out .runtime/resumed.json
```

第一条命令退出码 2，脚本已成功，保存失败，已花费 10。第二条复用脚本，仅补做保存，总计 12。注入标记持久化，同一活动只注入一次。

同一 ID 和相同输入再次执行，已完成任务直接返回缓存，new_steps 为 0。同一 ID 配不同输入返回冲突。修改预算、主题或截止时间时使用新 ID。未完成状态默认返回已有记录，显式 --resume 才开启下一轮。

## 输出及退出码

| 字段或退出码 | 含义 |
| --- | --- |
| status | running 为等待外部动作，completed 为已保存，incomplete 为未完成，error 为输入或存储冲突等错误 |
| steps | 四类工具的聚合状态，pending、succeeded 或 failed |
| trace | 每次调用的实际状态，失败事件永久保留 |
| charges / total_cost | 已成功收费工具的账单与合计 |
| saved_plan | SQLite 已保存方案的 JSON 投影 |
| reason | 未完成原因或错误原因 |
| 0 | 已完成或 bridge 正在等待动作 |
| 2 | 未完成，含缺字段、无可行方案、工具/模型问题或步数上限 |
| 3 | 命令输入错误、检查点冲突或存储/导出错误 |

使用 --out 导出完整 JSON。权威状态保存在 --db 指定的 SQLite 文件中，默认 .runtime/campaigns.sqlite。输出文件导出失败可使用相同 ID 重试，已提交的工具步骤会被复用。

## 项目目录

| 路径 | 内容 |
| --- | --- |
| src/content_agent/ | CLI、决策循环、模型适配、工具、SQLite 检查点 |
| tests/ | 业务回归、真实 CLI 子进程、并发与本机 HTTP 联测 |
| data/catalog.json | 合成素材与费用耗时表 |
| examples/inputs/ | 可复制输入 |
| examples/observed/ | 实际运行输出及 Trace |
| examples/test-results.txt | 实际测试日志 |
| examples/reproduce.py | 经正常 CLI 重建离线用例 |
| docs/ | 五份考核文档 |

本地工作由 AI 辅助完成。候选人的个人修改、理解验证和额外用时须据实际补充。项目当前覆盖的边界与待验证项见评估文档。

## 本轮加固

当前测试共 57 项，54 项通过，3 项本机 HTTP 联测因沙箱禁止 TCP 连接而跳过。已验证真实 CLI 的预算与失败重规划、四进程并发去重、导出路径保护、原子导出、极端时间错误和费率摘要检查。详情与限制见 docs/03-evaluation.md。

--out 会拒绝输入、动作文件和数据库的同路径覆盖，并通过原子替换保存完整导出。检查点新增素材/费率内容摘要，同版本号下内容改变也会返回冲突。旧检查点在下一次成功提交时补充摘要。

## 批量效果评估与人工评分

```powershell
python -m examples.evaluate --repeats 3 --out .runtime/evaluation
python -m examples.evaluate --model api --repeats 3 --out .runtime/api-evaluation
python -m examples.evaluate --review-file .runtime/evaluation/human-review.json
```

api 评估使用前文所述环境变量，可能产生供应商调用费用。默认 mock 评估使用标准用例，每次重复使用独立数据库，覆盖正常、预算重规划、工具失败、缺字段、无素材、部分素材、重复及两种续跑。

历史离线评估在 examples/evaluation/summary.json。该轮 14 个场景重复 3 次，42/42 预期匹配、42/42 结构检查通过，24 个完成，其余为预期未完成的负例或中断阶段。该结果描述固定规则的工程流程。

结构检查覆盖账单、预算、素材引用存在性、步骤序号、方案保存和制作时序。验收预期还要求对应的错误原因或关键工具观察，防止把模型错误误算成正确停止。

人工评分规则在 data/review-rubric.json，生成的 human-review.json 默认全为 null。实际评审人填写主题相关性、素材忠实度和可执行性三项 0–4 分，并注明姓名与依据，验证命令才会输出均分。当前人工评分待完成。

只有一个可用版本且生成暂时失败时，--resume 现在会重新允许该版本，复用已有素材和报价。参见 examples/observed/retry_interrupted.json 与 retry_completed.json。

## 十二类要求核对与审计输出

详细对照见 [十二类要求自检](docs/06-requirements-audit.md)。当前结果包含 task_plan、plan_versions、replanning、attempts、snapshots、run_id、task_id 和 unfinished。每个计划节点含 depends_on、输入引用、预计及实际费用耗时、状态和尝试引用。

```powershell
python -m src.content_agent --describe-tools
python -m src.content_agent examples/inputs/normal.json --catalog data/catalog.json --db .runtime/custom.sqlite --as-of 2030-05-01T09:00:00+08:00
python -m src.content_agent examples/inputs/partial_retry.json --partial-once --db .runtime/partial-retry.sqlite --as-of 2030-05-01T09:00:00+08:00
```

--catalog 接收合成素材与价格时间表，字段结构沿用 data/catalog.json，并校验金额、耗时、素材类型及 ID 唯一性。完整内容摘要用于阻止检查点混用不同费率。

新增逐项部分成功场景会保留成功素材，只按失败 ID 补取。费用规则为每任务一次检索包，补取免费。重复保存以及重复补取已成功素材均返回 reused，新增费用为零。

最新三轮离线核对在 examples/audit-evaluation/summary.json，18 个调用场景共 54 次，54/54 验收预期与结构检查通过。历史 examples/evaluation/ 记录保留。最新测试为 57 项中 54 项通过，3 项本机 HTTP 联测受限跳过。

本次带审计字段的外部模型实际路径为 examples/observed/audited_live_budget.json 和 audited_live_error.json。前者覆盖部分成功恢复及超预算换方案，后者覆盖工具失败后保留成功结果并换方案。供应商 API 自动端到端仍待验证。
