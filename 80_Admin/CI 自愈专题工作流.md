---
type: system
status: active
created: 2026-08-11
updated: 2026-08-17
domain: ci-self-healing
tags:
  - admin
  - workflow
  - ci-cd
  - ci-self-healing
ai_access: true
ai_generated: true
reviewed: false
source_notes:
  - "[[知识库工作流]]"
  - "[[AI 使用边界]]"
  - "[[来源约束知识规则]]"
---

# CI 自愈专题工作流

> [!abstract]
> 本流程是通用 [[知识库工作流]] 的专题扩展，只约束 `domain: ci-self-healing` 的笔记。它不改变其他技术、人文、日记或个人思考的组织方式。

主题入口：[[CI 自愈 MOC]]<br>
活跃项目：[[CI 自愈研究与方案]]<br>
动态工作台：[[CI 自愈工作台.base]]<br>
权威边界：[[来源约束知识规则]]

## 1. 笔记职责

| 通用类型 | 专题子类型 | 职责 |
| --- | --- | --- |
| `source` | `official-doc`、`engineering-blog`、`paper`、`repository`、`talk` | 保存来源信息、忠实摘录、原作者主张与公开边界 |
| `evergreen` | `case`、`mechanism`、`synthesis`、`framework` | 保存由公开原文直接支持的可复用知识 |
| `project` | `hub`、`solution-candidate`、`architecture`、`adr`、`experiment` | 积累候选机制，推进自己的方案、决策与验证 |
| `moc` | `topic-map` | 维护主题理解和人工导航 |

所有专题笔记使用：

```yaml
domain: ci-self-healing
```

专题中的 `Sources/` 与 `Knowledge/` 均为来源约束目录：禁止推断、个人判断和自有方案。`personal-insight` 不能放入这两个目录，应进入明确的非严格目录。

## 2. 状态约定

- Source：`inbox → approved → processed → stale / archived`
- Evergreen：`draft → active → superseded / archived`
- Solution Candidate：`captured → discussing → shortlisted → adopted / rejected / deferred`
- Architecture：`idea → draft → review → validated / superseded`
- ADR：`proposed → accepted / rejected / superseded`
- Experiment：`planned → running → passed / failed / inconclusive`

`reviewed` 表示当前内容是否经过人工确认。AI 修改正文后必须设为 `false`；人工确认后改回 `true`，但 `ai_generated` 继续保留。

## 3. 发现与人工准入

1. 你发现博客、论文、仓库或演讲后，可以先放入 `00_Inbox/CI 自愈候选/`。
2. 使用“评估这篇资料 `<URL/PDF>`”，让 AI 只检查作者或机构、技术深度、相关性、时效性与可追溯性。
3. 只有明确说“确认导入”后，才建立正式 Source 和原文快照，并将状态改为 `approved`。
4. 未确认材料继续留在 Inbox；AI 不得自行准入。

## 4. 保存原文与版本

- 博客保存清洗后的原语言 Markdown，论文保存原始 PDF。
- 快照文件名使用 `YYYY-MM-DD - 作者或机构 - 标题`。
- Source 记录原 URL、发布时间、核验日期、刷新周期、快照链接与 SHA-256。
- 来源发生实质变化时创建新快照，旧快照和指纹保持不变。
- Preview、Beta 或快速变化的产品资料默认 30 天复核；稳定产品文档或工程博客默认 90 天；论文和稳定机制默认 180 天。

## 5. 协同整理单篇资料

使用“分析来源：笔记名；重点：……”启动分析。固定检查：

1. 原作者解决的问题。
2. 技术机制、输入上下文和执行动作。
3. 权限、人工确认和宿主平台门禁。
4. 验证路径、失败退出和回退方式。
5. 产品或功能生命周期。
6. 效果证据、测量方法和未公开指标。

AI 先形成来源约束草稿，你负责逐条核对原文位置、适用条件和表述范围。只有明确说“确认分析”后，Source 才能进入 `processed` 与 `reviewed: true`。准入评价留在 Inbox，不写入正式 Source。

## 6. 沉淀来源约束知识

当公开机制可以脱离单篇 Source 重复使用时，建立 Evergreen：

- `case`：还原单个业界实践。
- `mechanism`：抽象可复用的技术机制。
- `synthesis`：综合多个来源或观点。
- `framework`：形成比较、评估或决策框架。
- `framework`：只保存公开标准或公开原文明确给出的框架；自建框架进入 Architecture。

每篇严格 Evergreen 只允许“直接事实、厂商主张、证据缺口、来源冲突”。一项权威一手来源可以支撑其明确描述的普通技术事实；缺少独立测量的厂商效果描述只能标为“厂商主张”。推理、反例选择、最终判断和方案启发进入 Architecture、ADR 或 Experiment。

## 7. 积累和筛选方案候选

`10_Projects/CI 自愈研究与方案/Candidates/` 保存从公开机制中得到、但尚未决定是否采用的独立技术方案候选。

1. 候选按“要解决的问题或可复用机制”建立，不按文章来源建立。一篇洞察可以产生多个候选，同一个候选也可以由多个 Knowledge 支撑。
2. 外部机制事实只在 Source 或 Knowledge 中维护；候选通过 `source_notes` 反链证据，不复制或扩张原文命题。
3. 候选正文记录问题、借鉴方式、本地适用条件、替代方案、冲突、待验证问题和决策进展，并明确标记为自有设计候选。
4. `captured` 只表示已经收集，不表示内容已经人工确认或方案已经采用；`reviewed` 也只表示当前笔记经过人工确认，不代表通过技术取舍。
5. 候选进入 `shortlisted` 前，必须明确问题、证据链、适用条件、替代方案和关键不确定性。
6. 需要事实验证的不确定性进入 Experiment；涉及边界、权限、状态、验证或实现路线的取舍进入 ADR。
7. 候选只有在关联 ADR 为 `accepted` 后才能进入 `adopted`。正式 Architecture 通过 `candidate_notes` 引用已采用候选。
8. `rejected` 和 `deferred` 候选继续保留原因、复审条件和替代关系，避免后续洞察触发重复讨论。

候选笔记统一使用：

```yaml
type: project
subtype: solution-candidate
status: captured
content_policy: design
source_notes: []
alternative_notes: []
experiment_notes: []
decision_notes: []
architecture_notes: []
```

## 8. 推进自有架构设计

1. Architecture 记录目标、约束、质量属性、组件职责、数据流、控制流、故障模式和安全边界。
2. 外部事实链接 Source 或 Evergreen；没有外部依据的内容标记为“本地假设”或“设计选择”。
3. 影响组件边界、权限、状态模型、验证策略或发布门禁的选择建立 ADR。
4. 对兼容性、性能、可靠性或安全存在不确定性时建立 Experiment。
5. 无需实验时，必须在 ADR 中记录免验理由，不能直接留空并接受决策。

## 9. 本轮研究收尾

使用“本轮研究收尾”让 AI 做只读检查并生成待办：

- 待确认的 AI 草稿。
- 缺少原文快照、指纹或来源链接的笔记。
- 已超过刷新周期的资料。
- 事实、厂商主张、推断、缺口和冲突是否混写。
- 新洞察是否产生了尚未登记的方案候选。
- 候选是否缺少适用条件、替代方案、实验或 ADR 去向。
- 未关闭的 ADR 与 Experiment。
- MOC 中需要更新的当前判断与开放问题。

## 10. 知识可用门槛

- Source：已人工准入、已保存快照或说明例外、分析已确认。
- Evergreen：引用 Source，全部命题受公开原文直接约束，无推断或自有方案，`status: active` 且 `reviewed: true`。
- Solution Candidate：引用对应 Knowledge，明确区分公开事实与自有设计，并记录状态、适用条件、替代方案和待验证问题；`adopted` 必须关联已接受 ADR。
- Architecture：关键判断可回溯；进入 `validated` 的架构只整合已采用候选，重要 ADR 已处理，不确定性有实验结果或免验说明。
- `authority: conversation` 的材料只能作为线索，不能独立支撑外部技术事实或架构决策。
