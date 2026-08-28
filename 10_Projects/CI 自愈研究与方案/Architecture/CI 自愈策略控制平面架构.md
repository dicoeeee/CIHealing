---
title: "CI 自愈策略控制平面架构"
type: project
subtype: architecture
domain: ci-self-healing
status: idea
created: 2026-08-12
updated: 2026-08-12
tags:
  - ci-cd
  - ci-self-healing
  - architecture
  - policy-engine
ai_access: true
ai_generated: true
reviewed: false
source_notes:
  - "[[CI 自愈控制机制 - 修复作用域与否决门禁]]"
decision_notes: []
experiment_notes: []
content_policy: design
---

# CI 自愈策略控制平面架构

> [!warning] 设计边界
> 本文是从 Nx 公开配置面抽取问题后形成的自有控制平面，不代表 Nx 已公开 Policy Loader、Resolver、PDP、Patch Guard 或 capability-based writeback。

## 设计问题

将“能否生成修复”“候选可以修改什么”“需要哪些验证”“能否写回”分成独立授权，避免 task allowlist 被误用为 patch 或 VCS 权限。

## 决策阶段

| 阶段 | 决策对象 | 结果示例 |
| --- | --- | --- |
| PR eligibility | repository、PR、branch、draft | allow / deny generation |
| Failure eligibility | normalized task identity | eligible / never-fix |
| Candidate scope | actual diff、paths、size、dependency changes | allow / review / deny |
| Verification | candidate fingerprint、plan、receipt | verified / rejected / expired |
| Writeback | head SHA、actor、action、policy snapshot | manual / auto / deny |

## 组件

| 组件 | 责任 |
| --- | --- |
| Identity Normalizer | 生成稳定 TaskRef、PRRef、PathSet 和 CandidateRef |
| Policy Loader | schema、版本、来源与签名检查 |
| Policy Resolver | 合并组织、仓库和 runtime policy |
| Policy Decision Point | 按阶段输出 decision、matched rule 和 reason code |
| Patch Guard | 从实际 diff 校验 changed paths、size、file types 与 symlinks |
| Verification Gate | 校验 receipt 与 candidate、HEAD、环境和时效的一致性 |
| Writeback Gateway | 仅消费短期单用途授权并返回 commit/push receipt |
| Audit Sink | 保存 policy snapshot、input fingerprint、decision 与 receipt |

## Effective policy

建议采用以下合并规则：

- organization deny 不可被 repository 或 runtime override 放宽；
- repository allow 只能在 organization allow 内收窄；
- runtime override 默认只能收窄；
- parse error、unknown matcher 或 missing required policy 时 deny；
- writeback 前重新计算 HEAD、policy 和 candidate scope。

## Decision output

```yaml
decision:
  phase: writeback
  result: require_manual_review
  reason_codes:
    - AUTO_APPLY_PATTERN_NOT_MATCHED
  policy_hash: sha256:...
  candidate_fingerprint: sha256:...
  head_sha: ...
  matched_rules: []
  expires_at: ...
```

Decision 不只返回 boolean，便于审计、重放和解释配置冲突。

## 失效模式

- positive/negative glob 语义不明确；
- unset、empty 与 default 三态丢失；
- include 与 deny 冲突；
- generation 后 policy 或 branch 状态漂移；
- path case、Unicode、symlink 或 rename 绕过；
- verification receipt 未绑定 candidate fingerprint；
- 多个 failures 共享一个 candidate，资格边界不明确；
- manual apply 与 auto-apply 复用宽权限凭据。

## 需要 ADR 的问题

- matcher grammar 与 normalization 规范；
- runtime override 是否可以扩大范围；
- multi-failure candidate 的授权单位；
- manual、auto 与 local apply 的身份模型；
- audit event 的存储与保留期。

## 需要实验的问题

- 现有 CI task names 的 normalization 冲突率；
- path guard 对真实候选的阻断和误报；
- writeback 前二次决策的 TOCTOU 发生率；
- reason codes 对人工 review 时间的影响。

## 证据来源

- [[CI 自愈控制机制 - 修复作用域与否决门禁]]
- [Nx Self-Healing CI configuration](https://nx.dev/docs/features/ci-features/self-healing-ci#configuring-self-healing-ci)
