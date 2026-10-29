# 03 效果验证

## 环境与实际结果

真实决策由本地 Qwen3-4B-Instruct-2507 Q4_K_M 生成，llama.cpp 在 WSL2 / Ubuntu 24.04 的 CPU 上运行，没有使用云端 API 或 Mock 回退。模型权重校验值、推理参数、运行窗口及调用数见 [runtime.json](../examples/real/runtime.json)。

- 自动化测试：**24/24 通过**，见 [test-results.json](../examples/test-results.json) 与 [逐项报告](../examples/test-results.txt)。
- 离线验收：**9/9 通过**，见 [mock/evaluation.json](../examples/mock/evaluation.json)。只证明工具和流程约束。
- 真实模型验收：**9/9 通过**，见 [real/evaluation.json](../examples/real/evaluation.json)。共 12 次场景执行、53 次真实模型决策调用，Agent 总执行时间 375.282 秒。重复提交包含四次连续运行，步数上限另执行一次。

这里的“通过”指本次合成场景满足验收断言，不表示模型在真实业务中的总体准确率。保存失败和上限场景预期返回失败/未完成，它们的验收仍可通过。

| 场景 | 实际状态（按运行顺序） | 决策步数 | 验收 |
| --- | --- | --- | --- |
| normal | COMPLETED | 5 | 通过 |
| missing_info | NEEDS_INFO | 3 | 通过 |
| rule_risk | BLOCKED | 3 | 通过 |
| time_conflict | PENDING_CONFIRMATION | 5 | 通过 |
| save_failure | FAILED | 5 | 通过 |
| supplemented | COMPLETED | 6 | 通过 |
| review_confirmation | PENDING_CONFIRMATION | 5 | 通过 |
| duplicate_submission | COMPLETED / COMPLETED / PENDING_CONFIRMATION / PENDING_CONFIRMATION | 5/5/5/5 | 通过 |
| step_limit | INCOMPLETE | 1 | 通过 |

## 可复现命令

先按根 README 启动模型服务，再执行：

```bash
python3 -m tests.run_and_record
python3 -m src.cli evaluate --mode mock
python3 -m src.cli evaluate --mode real
```

CI 运行自动化测试和离线验收。真实模型记录是在本机实际运行生成，CI 不下载模型。

## 验收映射与动态路径

| 验收点 | 输入、实际输出、Trace |
| --- | --- |
| 正常排期 | [normal](../examples/real/normal/) |
| 信息缺失 | [missing_info](../examples/real/missing_info/) |
| 规则风险 | [rule_risk](../examples/real/rule_risk/)、[review_confirmation](../examples/real/review_confirmation/) |
| 冲突与确认 | [time_conflict](../examples/real/time_conflict/) |
| 重复、占用变化 | [duplicate_submission](../examples/real/duplicate_submission/) |
| 保存失败 | [save_failure](../examples/real/save_failure/) |
| 补查后通过 | [supplemented](../examples/real/supplemented/) |
| 步数上限 | [step_limit](../examples/real/step_limit/) |

每个运行目录包含 input.json、output.json、trace.json；场景目录另有 stored-drafts.json。
Trace 中的 next_action 是下一轮模型实际选择的动作，不是预写的计划。

可比较正常路径中的“查询、保存”和规则风险路径中的“审核后结束”。补查场景根据读到的缺失字段及可用补充资料再次读取；冲突场景建议 19:00 并保存待确认草稿。重复请求先返回 UNCHANGED，18:00 被占用后更新为 19:00，再占用 19:00 后更新为 20:00，全程草稿 ID 相同、仅一行记录。原始硬性要求始终是 18:00。

## 失败、修正与复测

保留了两个真实运行问题：

1. [资料缺失时错误尝试保存](../examples/observed-failure/missing_info/trace.json)：模型曾将 NEEDS_INFO 当成可确认风险，工具以 REVIEW_REQUIRED 拒绝保存。修正状态说明，并在验收中增加“未通过审核不能选择查询/保存”的路径检查，避免最终结果正确掩盖错误决策。
2. [补查时生成错误素材编号](../examples/observed-failure/supplemented/trace.json)：模型生成 M0-than，工具拒绝。将结构化输出的素材 ID 限制在请求枚举后复测通过。第二轮完整评估曾为 8/9，失败的是该补查场景；最终完整复测为 9/9。

两个目录均保存对应输入、输出、Trace 和 assessment.json。业务保护与模型决策质量分别评价：拒绝错误写入不等于模型正确。

## 时间与范围

首版目标按 3 小时核心交付控制。可核验的本地代码建立至本轮验证整理窗口为 **2026-10-29 10:06:36 +0800 至 2026-10-29 11:09:12 +0800**，约 63 分钟，包括模型下载、构建和等待；该窗口是自动执行耗时，不等同于提交者个人工时。此前需求沟通和环境摸排未纳入该窗口，人工投入未独立计时。

已完成：本题六项业务验收、真实模型动作/观察循环、步数限制、离线入口、测试、实际记录和交付文档。
未接入：真实内容审核、真实发布服务、生产排期存储、界面。它们不属于本次核心实现范围。

## 已知限制

- 资料、规则、占用和故障均为本地合成，不验证真实版权或平台服务。
- 小模型仍可能选错动作；合法 ID 和 JSON 约束不保证决策正确，工具守卫和执行上限不可省略。
- SQLite 事务及文件版本复核不是分布式锁；外部排期系统应使用服务端版本号及原子预占。
- 未自动恢复进程中断的 Agent 会话。草稿持久化保留，重提相同请求会重新核验并防重保存。
- 默认 16 步未必足以处理大批素材，可在 1..100 范围调整；达到上限明确返回 INCOMPLETE。
- Ollama 适配器已实现，但本次真实验收使用 llama.cpp；不把未运行的后端描述为已验证。
