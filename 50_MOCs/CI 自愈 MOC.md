---
type: moc
subtype: topic-map
domain: ci-self-healing
status: active
created: 2026-08-11
updated: 2026-08-24
tags:
  - ci-cd
  - ci-self-healing
  - moc
ai_access: true
ai_generated: true
reviewed: false
source_notes: []
---

# CI 自愈 MOC

> [!abstract]
> 本页维护你对 CI 自愈领域的主题理解，不替代 Source、常青知识或项目设计。动态库存由 [[CI 自愈工作台.base]] 管理。

项目入口：[[CI 自愈研究与方案]]<br>
专题流程：[[CI 自愈专题工作流]]<br>
权威边界：[[来源约束知识规则]]

## 领域问题

- 如何区分偶发失败、环境失败、代码失败与未知失败？
- 如何在有限上下文中定位根因并生成候选修复？
- 如何证明修复有效，同时避免把任务重试误当成完整 CI 验证？
- 如何约束 Agent 的写入、合并、发布和凭据访问？
- 如何建立可观测、可评估、可回退的长期反馈闭环？

## 核心机制

- [[CI 自愈触发机制 - nx fix-ci 末端触发步骤]]
- [[CI 自愈上下文机制 - Project Graph]]
- [[CI 自愈上下文策略 - SELF_HEALING 指令]]
- [[CI 自愈控制机制 - 修复作用域与否决门禁]]

## 代表案例

- [[Harness CI]]：对话来源整理，尚需一手资料核验。

## 我的观点

## 方案候选

- [[仓库级语义指令作为修复上下文]]
- [[图谱驱动的失败上下文裁剪]]

## 自有架构

- [[CI 自愈问题诊断与修复方法论]]
- [[末端触发与修复编排架构]]
- [[CI 自愈策略控制平面架构]]

## 开放问题与证据缺口

## 动态工作台

![[CI 自愈工作台.base]]
