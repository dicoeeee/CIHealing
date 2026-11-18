---
ai_generated: true
reviewed: false
---

# session-delivery-with-changes

版本 1。用于双目录、单源码仓的 CI 回放。每次结束均交付以下两份 UTF-8 JSON 文件。两份文件可以独立生成和核对。

## 必交文件 triage.json

路径为 `output_dir/triage.json`。对象中的 `result` 填正式报告的最终枚举，`jobs` 填本次目标的原始数字 Job ID 整数数组，`next_step` 填接手人可执行的下一步。若没有已绑定 Job，使用 `{"result":"unresolved","jobs":[],"next_step":"补充失败 Job 身份"}`。实际解析最终文件，核对字段、原始 Job 身份及报告结果，完整读回。

## 必交文件 file_change.json

路径为 `output_dir/file_change.json`。本测试规范采用顶层字符串数组，每项为相对源码仓根目录的 POSIX 路径。同一路径只列一次，顺序不限。列出相对本次介入基线实际新增、修改或删除的源码与配置，排除 Git 元数据和构建目录 `target`。确认无净修改时，合法内容为 `[]`。

实际解析最终文件，对照现场净修改核对清单，完整读回。若无法确认变更范围或无法写入文件，在正式报告中说明对应缺口，继续交付其余独立文件。
