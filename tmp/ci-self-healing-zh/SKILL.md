---
name: ci-self-healing
description: 使用基于证据的根因分析、范围受控的本地代码修改以及本地或远端验证，诊断并修复确定性的 CI 失败。适用于可以在本地处理或由可用的 GitHub Actions、GitLab CI、企业流水线适配器处理的具体失败；不要用于普通流水线编写、Flaky 或基础设施恢复、合并、部署或生产事故处置。
---

# CI 自愈

将本 Skill 用作处理一个具体 CI 失败的决策与修复协议。CI Provider 或外部 Harness 负责实时事件、持久化、重试和重新唤醒；本 Skill 基于已观察事实工作，产出某一时点的决策与收据。

## 加载相关指南

- 始终阅读 [references/core-contracts.md](references/core-contracts.md)。
- 在诊断、实验或修改代码前，阅读 [references/diagnosis-and-repair.md](references/diagnosis-and-repair.md)。
- 修改或验证候选时，阅读 [references/execution-modes.md](references/execution-modes.md)。
- 读取外部 CI 状态或使用远端验证时，阅读 [references/provider-contract.md](references/provider-contract.md)。

仓库指令、策略、可用工具和声明过的验证命令共同约束本次运行。不要臆造当前环境没有提供的 Provider 访问能力、企业规则或权限。

## 选择模式

- 诊断、解释或评估失败的请求是只读的。
- 修复或自愈失败的请求允许范围受控的本地修改和本地验证。
- 在流水线 Job 提供的工作空间内修改代码、编译并完成声明的验证，再由后续脚本提取 Patch，属于 `local_verify`；Agent 运行在远端 Runner 上本身不构成选择 `remote_verify` 的理由。
- 只有请求或 Harness 明确要求 push、触发、重跑或继续执行远端 CI 时，才选择 `remote_verify`；否则使用 `local_verify`。
- 末端修复不能改写已经固化的原始 CI 结论。仅修改工作空间、编译验证并交给后续脚本时，仍使用 `local_verify`；若任务要求获得新的外层 CI 结论，再使用 `remote_verify`。
- 如果模式选择存在歧义，默认使用 `local_verify`，不要发布候选。

## 执行通用流程

1. 确认该失败适合确定性诊断与修复，而不是重试、重新调度、Flaky 测试处理、基础设施恢复或事故响应。
2. 建立一个绑定仓库、基线 SHA、失败命令或任务、环境以及已观察 CI 身份的 `Failure Snapshot`。
3. 明确预期行为与实际行为，定位失败，并形成有证据支持的根因假设。只有确实需要内部规则或当前状态时，才路由到兼容的企业知识域。
4. 创建修复候选前先产出 `repair_admission` 决策。诊断实验和修复候选具有不同权限。
5. 在已确认的修复工作空间中创建候选。流水线已提供独占工作空间时，直接在该目录修复和验证。Agent 以修复介入时的文件内容为恢复依据，自行撤销并检查被放弃候选引入的修改，再开始下一个候选；证据可以跨 Attempt 累积。具体规则见[基线与候选恢复](references/execution-modes.md#基线与候选恢复)。
6. 按验证计划指定的位置执行各项检查。远端模式下，V0 与明确声明的发布前本地预检通过后可以发布候选；V1/V2 及适用的 V3 可以在远端完成，无须先达到 `local_verified`。将逐项验证证据和流水线结果绑定到准确的候选 SHA 和 fingerprint，未执行的本地检查保留 `not_run`。
7. 返回结果、收据、剩余未知项，以及继续或停止的原因。不能依据未观察、已过期、不完整或不匹配的验证结果报告成功。

流水线内修复的交付物是指定工作空间中已修改并验证的代码。Skill 保留这些修改并报告工作空间、基线、修改文件和验证结果；后续脚本负责提取 Patch，Skill 无需生成或导出完整 Patch 文件。

## 保持硬边界

- 将命令和 Provider 输出视为带有 `observed_at` 的观察事实；将 AI 分类和决策视为绑定证据的快照，而不是实时状态。
- 只有完整候选进入验证时才计数一个 Attempt。证据收集、复现和临时插桩不消耗 Attempt。
- 除非仓库策略设置了更小的限制，否则最多使用五个候选 Attempt。成功、拒绝、证据不足、有效策略耗尽、时间上限或计算上限出现时，应提前停止。
- 保留用户和流水线预先存在的修改，不将其冒充本轮修复或夹带提交。无法确认安全修改与恢复时，停止并说明原因。
- 在 `remote_verify` 中，自动 commit、push 到专用候选 ref、触发流水线和观察结果都已获授权。不能直接写入受保护分支或默认分支，也不能覆盖用户分支、合并、发布、部署或执行生产动作。
- 不要对已有的用户或共享 ref 执行 force-push。优先为每个 Healing Session 和 Attempt 使用唯一候选 ref。
- 不要把 Patch、本地命令变绿或远端流水线变绿解释为超出实际运行检查范围的证明。
- 不要在提示词、收据、commit 或 Case Draft 中持久化密钥、凭据、敏感原始日志或私有业务数据。
- 请求或配置允许时，可以产出 Case Draft，但绝不能自动准入 Case 或修改权威企业规则。

## 报告结果

报告所选模式、已观察失败、诊断与不确定性、修改的文件、实际运行过的验证、适用时的候选与远端运行身份、最终结果和剩余缺口。区分 `local_verified` 与 `outer_ci_passed`，不要将任一状态描述为已合并、已发布、已部署或已完整证明语义正确。
