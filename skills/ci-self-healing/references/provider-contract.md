# Provider 合同

核心 Skill 与 Provider 无关。平台适配器将 GitHub Actions、GitLab CI、企业 CI 或分离的 SCM/CI 系统转换为同一套观察事实和有界动作。

不要仅因为此处列出了平台名称，就声称已经支持该平台。只有在其适配器或可用工具满足并通过相关合同后，才可以声明支持该 Provider。

## 访问方式与合同的关系

日志和运行信息的访问选择、CLI 帮助发现与缺口处理统一见 [Provider 访问](providers/provider-access.md)。证据读取不替代本合同中的候选发布、触发、观察和验证能力。

下表定义要取得的观察事实或可执行的动作，不要求固定工具名、封装函数或“能力 → 子命令”的逐条映射。Agent 可通过一个或多个获准调用满足当前所需能力，以实际返回的身份、证据和动作结果核对是否满足合同。

## 能力协商

适配器可以暴露以下能力：

| 能力 | 目的 |
| --- | --- |
| `identify_run` | 解析仓库、流水线、Attempt、Job、任务、commit 和触发身份 |
| `inspect_run` | 读取当前观察到的流水线、Job 和检查结果 |
| `fetch_failure_evidence` | 获取日志、注解、制品、退出码和完成证据 |
| `publish_candidate` | 通过受限 SCM 路径 commit 或发布准确候选 |
| `trigger_pipeline` | 发布不会自动触发流水线时，dispatch 一次流水线 |
| `watch_pipeline` | 轮询、订阅或以其他方式观察运行直到终态 |
| `fetch_verification_result` | 返回准确候选 SHA 对应的终态检查和证据 |

模式要求：

| 操作 | 所需能力 |
| --- | --- |
| 外部诊断 | `identify_run`、`inspect_run`、`fetch_failure_evidence` |
| 根据已提供证据进行本地修复 | 不要求远端能力 |
| 远端验证 | `publish_candidate`、运行解析或 `trigger_pipeline`、`watch_pipeline`、`fetch_verification_result` |

如果所需能力不可用，报告缺失能力及已观察事实，按请求边界进入 [自主推进与停止](diagnosis-and-repair.md#自主推进与停止)。能力未补齐前不执行依赖它的发布或验证，也不用网页 URL、过期日志或无关运行替代验证。MR、提交与特性材料的获取遵循[变更上下文与预期行为](diagnosis-and-repair.md#变更上下文与预期行为)，使用实际提供且获准的 SCM、Git 或仓库检索能力。

## 归一化 Provider 观察

```yaml
provider_observation:
  provider: <稳定的 Provider ID>
  repository: <稳定的仓库身份>
  execution:
    pipeline_id: <Provider 流水线或工作流 ID>
    run_id: <运行 ID>
    run_attempt: optional
    job_id: optional
    task_id: optional
    trigger: <push、dispatch、schedule、parent 或 Provider 专用触发>
  source:
    ref: <分支或候选 ref>
    head_sha: <准确观察到的 SHA>
  status: queued | running | passed | failed | canceled | unknown
  evidence_refs: []
  completeness: complete | incomplete
  observed_at: <时间戳>
```

Provider 原生状态名称必须在不丢失原始值的情况下映射。归一化映射不是一对一时，在适配器证据中保留原始状态。

在证据引用旁记录实际获取方式（`local_evidence`、`mcp` 或 `cli`）、来源路径或工具操作、所属运行/Job/重试，以及分页或截断等缺口；混合方式补齐时分别记录。方法表示如何取得证据，不改变原失败的 Provider。访问缺口按[不可用与交付](providers/provider-access.md#不可用与交付)返回诊断流程。

## 完成与身份规则

- 在 Provider 证明预期 Job 或检查已完成之前，不要将运行标记为健康。
- 缺失日志、取消的 Job、被跳过的 Required Job、截断的制品和不可用的第三方检查都必须明确记录。
- 修复验证时，按 [分层验证](repair-and-verification.md#分层验证)返回计划中各项检查的实际执行证据；Provider 汇总状态本身不作修复成功判定。
- 观察保留上方仓库、ref、head SHA、运行/Job 身份和时间；修复收据再按 [核心合同](core-contracts.md#收据绑定与失效)关联调用与候选。
- 如果一次 push 会自动触发目标流水线，则解析并观察该运行。除非配置要求另一条独立流水线，否则不要再调用显式 dispatch 端点。
- 使用本次 `invocation_id`（Healing Session）与 Attempt 身份标识动作，并结合原始运行、触发来源、候选 SHA 和已有运行检测自触发循环与重复投递。新调用独立计数不取消对同一发布或触发动作的幂等检查。

## 实时观察边界

适配器报告某个时刻观察到的内容；Skill 本身不提供持续同步。当前执行可在预算内等待或轮询；需要在流水线仍排队或运行时退出，则在获准的报告或宿主记录中保留运行引用、最近观察时间和未完成检查。只有宿主已有并授权的续接机制才能负责后续观察或重新调用；无该机制时按关键缺口规则结束并说明仍需观察，不声称会自动继续，也不自行建立 webhook、定时任务或唤醒服务。

当决策依赖当前状态时，如果没有刷新过期的 `running`、`passed` 或 `failed` 观察，不要将其变成当前结论。

## 企业适配器

企业适配器可以通过经过认证的 CLI、API、MCP Server、webhook 负载或任务运行器事件流实现。保持两个关注点分离：

- Provider 适配器说明如何识别、检查、发布、触发和观察；
- Domain Knowledge Pack 说明内部语义、当前业务状态、合法组合、修复约束和专项验证器。

Provider 访问能力不会自动提供企业语义。领域知识也不会授予 CI 或 SCM 权限。用户交互和 Agent 移交通道由宿主提供，其声明见[执行身份与协作能力](core-contracts.md#执行身份与协作能力)；列出本合同不表示已实现这些通道。

## 安全与信任

输入处理遵循主 Skill 的[硬边界](../SKILL.md#保持硬边界)。

- 使用最小权限、短时凭据，并将其限制到仓库、专用候选命名空间和所需的触发/读取动作。
- 将读取、候选发布、流水线触发、合并和部署保持为不同能力。本 Skill 只消费所选模式要求的前三种能力。
- 发布与触发仅在 [远端执行授权](remote-verification.md#授权与能力)满足后进行；能力协商不授予写权限。

## 适配器验收证据

仅实现日志读取的接入，可先按维护者的[接入验收](../README.md#接入验收)验证并声明已验证的读取能力；不据此声明支持远端发布或完整 `remote_verify`。下列完整适配器检查按实际声明的能力执行。

在声明适配器具备完整远端验证能力之前，至少测试：

- 对准确 SHA 的成功和失败运行进行识别；
- 多 Job 失败证据，以及不完整或不可用的日志；
- 本地缺少原失败运行环境时，由远端实际执行该检查并返回对应证据；流水线整体通过但原失败检查被跳过或未触发时，不得得出 `outer_ci_passed`；
- push 触发和显式 dispatch 流水线，且不能重复触发；
- 重复 webhook 或重试的幂等性；
- 取消、超时、权限拒绝和过期运行处理；
- 候选到运行的绑定，以及对不匹配 SHA 的拒绝；
- 凭据范围的正向和负向行为。

只有存在有依据的实际访问方式需要记录或测试时，才添加 Provider 专用 reference 或确定性辅助工具。文档示例、工具存在与运行验收分别说明；不要提前创建空的 Provider 文件或通用命令目录。
