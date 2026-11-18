---
ai_generated: true
reviewed: false
---

# session-delivery

版本 1。适用于本次双目录 CI 回放。

## 必交文件 triage.json

- 路径为 `output_dir/triage.json`。使用 UTF-8 JSON 对象。
- `result` 取正式报告的最终枚举，`jobs` 是本次目标的原始数字 Job ID 整数数组，`next_step` 是接手人可执行的下一步；已完成且无需追加动作时写“无”。结果与 Job ID 以本轮报告结论和现场原始身份为依据，不依赖报告文件的最终摘要。
- 即使没有源码变更也交付该文件。若无已绑定 Job，合法空内容示例为 `{"result":"unresolved","jobs":[],"next_step":"补充失败 Job 身份"}`。缺失部分 Job 身份只列已知 ID 并在下一步说明缺口；无法安全写入则在正式报告中说明。
- 无独立校验器。实际解析最终 JSON，核对字段类型、值和报告一致，并读回最终文件。
