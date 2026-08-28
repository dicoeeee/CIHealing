# CI Self-Healing Repository Rules

本仓库是 CI 自愈研究、文档、实验和项目代码的唯一后续工作目录。不要再把新的 CI 自愈内容写回原个人知识库。

## 内容与访问边界

- 处理 Markdown 前先检查 YAML frontmatter；若存在 `ai_access: false`，除非用户在当前任务中明确许可，否则不得读取、搜索、总结或修改。
- 不得写入或提交密码、API Key、令牌、身份资料、客户数据、公司机密或私密聊天。
- 保留用户原有写作语气、Markdown 结构、Obsidian 双链和来源路径。
- 不得把 `reviewed: false` 自动改为 `true`，也不得把候选状态自动提升为已采用或已验证。

## 来源约束

在处理 `30_Resources/**/Sources/` 或 `30_Resources/**/Knowledge/` 前，完整读取并遵守 `80_Admin/来源约束知识规则.md`。

- `Sources/` 只记录原文明确说了什么。
- `Knowledge/` 只记录公开一手来源能够共同确认的事实、厂商主张、证据缺口或来源冲突。
- 推理和设计放入 `10_Projects/**/Architecture/`。
- 取舍放入 `10_Projects/**/ADR/`。
- 待验证假设与验证结果放入 `10_Projects/**/Experiments/`。
- Patch、运行结果或厂商描述都不能单独作为修复正确性的完整证明。

## 安全变更

- 默认允许创建草稿、摘要、元数据、链接、评审记录、实验代码和测试。
- 不得在未先给出变更范围的情况下删除文档、覆盖大量用户正文或批量改造目录。
- 自动生成或实质修改的知识正文应保留 `ai_generated: true`，并将 `reviewed` 设为 `false`。
- 提交时只包含当前任务的连贯变更，排除本地状态、临时文件、密钥和无关生成物。
