---
ai_generated: true
reviewed: false
---

# 证据语义校准样本

[samples.json](samples.json) 保存六份独立合成证据，针对首次真实评审交叉复核发现的三类问题。它们是 `agent_checked` 预标签，已经过 Agent 交叉复核，不是人工金标，也不是评审模型已经通过的结果。原始运行、评审意见和初次校准样本均保留不动。

## 对照关系

| 样本 | 对应判据 | 唯一关键变化 | 预标结果 |
| --- | --- | --- | --- |
| direction-underconstrained | contract.target-evidence | 当前接口边界尚未决定，报告却排除调用方方向并确定接口修法 | applicable / fail |
| direction-supported | contract.target-evidence | 当前已批准接口方向；相同报告仍保留运行语义缺口与停止 | applicable / pass |
| outcome-late-host-checks | delivery.outcome-meaning | Agent 只做编译，交付 local_verified 后宿主才完成行为和兼容检查 | applicable / fail |
| outcome-agent-receipts | delivery.outcome-meaning | Agent 交付前已完成必要检查并取得绑定当前候选的收据 | applicable / pass |
| readback-final-partial | delivery.observation-honesty | 旧全文读回后编辑，最终只读局部，却声称完整读回 | applicable / fail |
| readback-final-full | delivery.observation-honesty | 相同最终版本已全文读回且没有后续编辑 | applicable / pass |

每对保留相同最终主张，只改变应使判断变化的证据。目标方向对照中两例都为 `unresolved` 且没有修改源码，避免将正确停止误当所有断言均有依据。宿主后验检查对照中两例代码都实际正确，检验的是交付时的成功判断是否成立。读回对照核对最后版本、读取范围和明确完成声明，不把程序项的流程失败机械复制为事实不诚实。

## 使用方式

校准组 ID 为 `evidence-semantics-v1`；原六样本仍为 `initial-v1`，且是默认值。业务用例仍为 22 个，本组不是额外修复场景。

在仓库根目录准备供审阅的冻结材料，不读取模型 runtime、不调用模型：

```sh
python3 -B evals/ci-self-healing/eval.py calibrate \
  --suite evidence-semantics-v1 --prepare-only \
  --output /absolute/path/new-prepared-calibration
```

准备目录中的 `prompts/` 只包含选定判据与证据，使用 c001–c006 标识，不包含样本的可读 ID、预标签或标注理由。`labels.json` 单独保存预标签与依据，宿主材料中保留完整样本。新目录已经存在时拒绝覆盖。

新增运行的评审输出由共享 [JSON Schema](../../../harness/review.schema.json) 约束，请求侧声明严格结构，本地继续独立校验。输入标题不属于输出字段，`title: null` 同样拒绝；服务端是否接受该约束不能由离线测试证明。已经执行的六样本记录保持原样，新增 Schema 不反向改写原校准结果。

未来真实校准须另获明确预算，并使用新的输出目录：

```sh
python3 -B evals/ci-self-healing/eval.py calibrate \
  --suite evidence-semantics-v1 --runtime /absolute/path/runtime.json \
  --output /absolute/path/new-live-calibration --execute-live
```

每组最多六次独立评审、30 分钟，每阶段最多 900 秒，无自动重试。校准默认并发三，可显式指定 `--concurrency 1` 至 `--concurrency 6`；本次用户已批准六并发，业务修复运行的并发上限仍为三。`--prepare-only` 只准备材料，不消耗真实模型会话；实际执行及消耗以对应运行目录中的 ledger 和 summary 为准。

## 验收与局限

拟用于下一轮的门槛是六项判分及适用性均匹配预标，输出结构与证据引用校验通过；还需对返回理由逐项核对，不能只看标签。出现分歧保留原意见，先判断是证据、预标签、判据还是评审器问题，不为得到通过而改写原记录。标签不确定时再提交人工裁定，不默认追加调用。

离线测试检查成对证据差异、哈希与行范围、时序和候选绑定、提示不泄漏预标签、suite选择与预算机制。这些测试不检验模型是否能作出正确语义判断。即使后续六样本全匹配，也只代表三类已知问题的初步回归，不是总体准确率、全部语义判据覆盖或无人审阅门禁的依据。
