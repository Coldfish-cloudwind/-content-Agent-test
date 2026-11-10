# 测试与效果验证

## 方法

验证由可执行测试、离线场景和外部模型逐步执行组成。所有实际输出均保存为 JSON，包含输入 ID、状态、账单、方案、步骤与 Trace。测试环境为 Windows、Python 3.13.12。

```powershell
python -m unittest discover -s tests -v
python examples/reproduce.py
```

第一条命令的实际日志为 [test-results.txt](../examples/test-results.txt)。第二条使用临时数据库重建离线输出到 .runtime/examples/，每个用例都经由 CLI 执行。当前交付版本保存在 examples/observed/。

## 预期与实际结果

| 输入 | 预期 | 实际输出 | 结果 |
| --- | --- | --- | --- |
| normal | studio 正常完成，12 | [normal.json](../examples/observed/normal.json) | completed，12 |
| normal 再次执行 | 缓存，零新步骤 | [repeat.json](../examples/observed/repeat.json) | cached，new_steps=0，12 |
| replan | 12 超过 10，改选 8 | [replan.json](../examples/observed/replan.json) | completed，lite，8 |
| failure | 制作失败后保留素材、改选版本 | [failure.json](../examples/observed/failure.json) | 失败记录保留，lite 完成，8 |
| missing_budget | 缺预算，零费用 | [missing_budget.json](../examples/observed/missing_budget.json) | incomplete/invalid_input，0 |
| missing_deadline | 缺截止时间，零费用 | [missing_deadline.json](../examples/observed/missing_deadline.json) | incomplete/invalid_input，0 |
| no_budget | 最低报价 8，预算 6，缺口 2 | [no_budget.json](../examples/observed/no_budget.json) | incomplete，保留检索费用 1 |
| short_time | studio 超时，改选 lite | [short_time.json](../examples/observed/short_time.json) | studio 超时 18 分钟，lite 完成，8 |
| no_assets | 检索无匹配，停止 | [no_assets.json](../examples/observed/no_assets.json) | incomplete，0 |
| partial_assets | 一张图片仅支持 lite | [partial_assets.json](../examples/observed/partial_assets.json) | completed，lite，8 |
| resume 第一轮 | 保存失败，达到 4 步上限 | [interrupted.json](../examples/observed/interrupted.json) | incomplete/step_limit，脚本保留，10 |
| resume 第二轮 | 只补做保存 | [resumed.json](../examples/observed/resumed.json) | completed，new_steps=1，12 |

以上是 mock 工程流程回归结果。输入文件名与首列一致，resume 的两次执行使用同一输入。具体注入和运行命令见 examples/reproduce.py 的 CASES 表。

## 外部模型动态决策

本次 Codex 通过 bridge 在实际执行期间逐轮决定动作，完整记录如下。每一步动作都在拿到上一步工具结果后提交，记录中的 reason 是公开简要依据。

| 输入与记录 | 关键工具观察 | 实际后续选择 | 最终结果 |
| --- | --- | --- | --- |
| [live_budget.json](../examples/observed/live_budget.json) | studio 报价 12，预算缺口 2 | estimate_cost(lite)，观察报价 8，再制作和保存 | completed，8 |
| [live_failure.json](../examples/observed/live_failure.json) | studio 制作返回 injected_unavailable，费用仍为 1 | 保留素材，询价 lite，再制作和保存 | completed，8 |
| [live_stop.json](../examples/observed/live_stop.json) | lite 报价 8，预算 6 | stop，说明缺口 2 | incomplete，费用 1 |

外部路径配置与离线路径一致，使用 --model bridge、--external-name Codex、固定测试时钟和独立数据库。live_failure 另加 --fail-once generate_script。bridge 需要外部模型逐轮提交，项目可以保存观察与动作，但无法通过日志独立认证模型身份。API 自动端到端决策尚无真实供应商运行证据。

## 测试覆盖

当前共 57 项测试，54 项通过，3 项本机 HTTP 联测因运行环境禁止 TCP 连接而跳过，日志见 examples/test-results.txt。覆盖金额与时序一致性、正常完成、字段缺失、预算不可行、工具失败后替代、重复执行、ID 冲突、断点续跑、截止时间重规划、过期资料、无素材和部分素材。

另覆盖单轮及累计上限、非法模型响应、过期 bridge 动作、并发 revision 冲突、依赖和预算守卫、推理后时间变化、API 请求协议与密钥不进入业务 Trace、模型失败退出，以及 CLI 正常和重复入口。

API 测试采用替身响应。它检查请求格式和错误处理，真实模型选择能力需在有凭据时通过 api 模式另行验证。

## 失败与限制

- 正常素材来自小规模合成标签库，检索质量与素材真实性尚未评估。
- 脚本由模板生成，尚无人工盲评、真实发布效果或创意质量量化结果。
- 费用与耗时为模拟表。失败不收费的约定尚不能直接套用到真实计费服务。
- SQLite 原子性覆盖本地工具；真实远端副作用需要额外幂等协议。
- 模型可能提前停止或选择不佳版本。当前只保证执行层守住预算、时序、状态及上限。
- 无效模型 JSON 会返回 incomplete/model_error，暂未实现自动格式修复。
- 本次样本用于验收覆盖，未统计模型多次采样的成功率或稳定性。

## 下一轮实验

使用真实模型 API 对所有主题和至少三档预算、三档时间窗口重复运行，分别统计完成率、约束违规率、工具失败恢复率、平均步数及 API 花费。由人工检查方案相关性、素材准确性与可执行性，保留原始观察、供应商模型名和运行时间，更新本表的预期与实际差异。

## 实机优化验证

本轮从 CLI 入口运行子进程，实际执行预算重规划、工具失败后的观察与再决策，并同时启动四个 CLI 进程访问同一活动。并发结束后检查最终费用为 12、账单三条、决策四步，未出现重复计费。

| 问题 | 修改 | 验证 |
| --- | --- | --- |
| HTTP 截断响应抛出未处理异常 | 捕获 HTTPException，转换为 model_error | 注入 IncompleteRead，结果明确未完成 |
| 极端时区日期溢出 | 转换为可读输入错误 | 真正 CLI 子进程返回 JSON error 和退出码 3 |
| 导出可覆盖输入或数据库 | 拒绝冲突路径 | 实际命令后文件原字节保持一致 |
| 导出中断可能破坏旧结果 | 同目录临时文件写入并原子替换 | 模拟替换失败，旧输出保留，临时文件清理 |
| 费率表内容变化但版本号未变 | 检查完整素材/费率摘要 | 修改同版本费率后拒绝旧检查点 |
| 旧检查点兼容 | 缺摘要的旧检查点按原版本规则加载，下一次提交写入摘要 | 老状态可继续完成 |
| API 地址配置错误 | 返回明确模型配置失败 | 无效 IPv6 地址保持完整错误记录 |

本机 HTTP 测试会启动确定性响应服务，并经真实 CLI 的 api 模式发起请求。当前沙箱的本机连接返回 WinError 10013，因此三项网络测试标记跳过。API 协议替身测试及 bridge 子进程测试通过，真实供应商 API 仍待可用凭据与网络条件验证。

旧检查点首次绑定新摘要时，无法倒推旧版本号相同的历史数据是否已被修改。这是兼容旧状态的边界，新的检查点从创建起校验完整摘要。

## 批量评估与评分入口

可运行 python -m examples.evaluate --repeats 3 --out .runtime/evaluation。真实模型入口为同一命令加 --model api，并预先配置环境变量。每次重复独立建库，同一轮内保留重复调用及续跑所需状态。

本次实际离线结果见 [summary.json](../examples/evaluation/summary.json)。14 个场景重复三次，共 42 条记录，验收预期匹配 42/42，结构检查通过 42/42，24/42 调用完成。完成率 57.14% 的分母包含缺字段、资料不足、无可行方案和故障中断等预期未完成场景。这里应优先看预期匹配率，而模型能力与稳定性仍需真实 API 实验。

结构评分检查金额、预算、素材引用存在、任务状态及时间一致性。预期匹配还检查必要的工具观察、错误原因以及重复/续跑的新步骤数，避免模型接口故障被误算成“正确处理无素材”。每条原始输出和检查结果都保存到 examples/evaluation/。

评分规则见 [review-rubric.json](../data/review-rubric.json)，人工表见 [human-review.json](../examples/evaluation/human-review.json)。主题相关性、素材忠实度、可执行性各 0–4 分。尚未人工评审时保留 null，评分程序输出 pending，不计算均分。评分工具的零分与非法值校验有专门测试。

## 单一候选版本的失败续跑

发现离线控制器原先会把历史生成失败的版本永久排除，导致只有 lite 素材时无法显式续跑。已改为仅排除当前执行轮次内的失败版本；外部模型上下文也增加 session 字段。原始失败历史继续保留。

- [retry_interrupted.json](../examples/observed/retry_interrupted.json) 中，单图片方案生成失败，保留素材，费用为 1，返回未完成。
- [retry_completed.json](../examples/observed/retry_completed.json) 中，--resume 后只新增生成和保存两步，总费用为 8，没有再次检索或扣检索费。

## 十二类要求补齐后的验证

最新测试日志为 examples/test-results.txt，57 项中 54 项通过，3 项本机 HTTP 联测受限跳过。新增测试直接检查 DAG 拓扑、每步估计与实际费用、结果契约、幂等键、计划拒绝与版本差异、失败记录不可变、部分成功逐项重试、前置失败阻止保存、自定义费用表以及旧检查点迁移。

最新批量结果见 [audit-evaluation/summary.json](../examples/audit-evaluation/summary.json)，18 个调用场景各重复三次，54/54 预期匹配和结构检查通过，30 次完成，其余为预期负例和中断阶段。历史 42 次记录保留于 examples/evaluation/，两份评估的场景集不同。

新场景证据包括 [partial_retry.json](../examples/observed/partial_retry.json)、[partial_retained.json](../examples/observed/partial_retained.json)、[partial_resumed.json](../examples/observed/partial_resumed.json)、[dependency_blocked.json](../examples/observed/dependency_blocked.json)。

本次外部模型逐步路径为 [audited_live_budget.json](../examples/observed/audited_live_budget.json) 与 [audited_live_error.json](../examples/observed/audited_live_error.json)。均通过 bridge 逐轮观察真实工具结果后执行，包含计划版本与不可变尝试。最初 live_ 文件作为早期历史记录保留。

完整十二类要求及具体测试名称见 [06-requirements-audit.md](06-requirements-audit.md)。真实供应商 API 和人工质量评分仍待验证。
