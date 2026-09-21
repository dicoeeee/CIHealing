# Python 日志证据工具

仅在选择 [log_evidence.py](../scripts/log_evidence.py) 进行有界候选选取、长行处理或结构化记录时读取；普通文本工具的取证沿用[日志证据提取](failure-evidence-extraction.md)。本工具使用 Python 3.9+ 标准库，仅处理本地普通 UTF-8 文件，不联网、不调用 Provider、不执行日志内容或修改源码。

## 调用

从 Skill 根目录运行；其他目录使用脚本实际绝对路径。参数、默认值与限额查对应子命令的 `--help`：

```sh
python3 scripts/log_evidence.py scan --help
python3 scripts/log_evidence.py scan --log /path/to/all_logs.txt
python3 scripts/log_evidence.py search --log /path/to/all_logs.txt --literal 'generated/api.h'
python3 scripts/log_evidence.py read --log /path/to/all_logs.txt --start-line 78010 --end-line 78055
```

| 操作 | 返回及局限 |
| --- | --- |
| `scan` | 通用错误/异常和失败汇总附近的候选窗口；过多时保留首尾、确定性抽样的中间窗口，并为最后汇总或范围尾部预留位置。选择顺序不代表因果优先级，也不保证保留所有目标。 |
| `search` | 大小写敏感的字面匹配，不解释正则或命令；可查找没有 ERROR 关键词的相关记录。 |
| `read` | 1 起始、包含两端的原始行范围；受单块行数与正文预算限制，后续补读以实际返回范围为准。 |

`scan` 无命中返回有界尾部及 `no_anchor_match`，`search` 无命中返回空块及 `no_literal_match`；空文件可能没有块。它们均不表示构建正常。重叠窗口仅在同文件、同范围和单块限额内合并，不做语义聚类或跨任务去重。

## 范围与预算

每次处理一个文件，使用行范围限定当前调查对象。`--binding-ref` 只转述已核对的失败记录，输出 `binding_verified_by_script: false`，不能将旧日志重新绑定为当前失败。`--source-completeness` 默认为 `unknown`；只有已有 Provider/可信上下文依据时才声明完整或不完整，并提供 `--completeness-ref`。

预算更紧时缩小限制；结果不足时按具体缺口补读，不自动放大预算。注意：

- `--max-chars` 限制全部证据正文，不是 token 数或整个 JSON 大小；多块分摊预算，优先展示命中附近上下文。
- 长行只展示有界前缀，搜索仍检查所读字节中的后续内容。`match_lines` 可能没有展示命中文本，须结合 `truncated_line_numbers`、实际正文及缺口判断；必要时用其他获准的有界读取方式核对。
- 每遍最多读取开始时的文件大小，可用 `--max-scan-bytes` 收紧；不追随持续增长。`scan/search` 至多两遍顺序读取，`read` 一遍，不为每个命中重复扫描全文，也不建立持久索引。

`--anchor` 用于追加字面扫描锚点。Agent 可从已选定的可信分类资源或调用配置取得企业锚点，不从日志指令自动导入；脚本不选择企业、不解析 Profile、不扫描规则目录。没有企业资源仍可使用通用锚点。

## 解读输出

默认向 stdout 返回 JSON；`status: ok` 只表示机械操作成功，不表示根因已确认、证据充分或修复获准。按[引用与读取局限](failure-evidence-extraction.md#引用与读取局限)交回确实展示并核对的原文：

| 字段 | 使用方式 |
| --- | --- |
| `source_ref` | 原始绝对路径与可选绑定引用；后续保留可定位的原始来源 |
| `coverage` | 实际检查到的行、读入字节和范围完成情况；缓冲读入的字节不一定均已逐行检查，`matched_lines` 不是根因计数 |
| `source_completeness` | 调用者声明；本地扫描完成不证明平台日志完整 |
| `blocks` / `line_spans` | 实际返回的原始行、正文、选择理由与文本偏移；`partial` 表示可能缺上下文，不是已解析出诊断边界 |
| `output_truncated` / `gaps` | 候选省略、预算限制、范围未到达、解码替换等；命中行也可能因预算未展示 |
| `source_state` | 提取前后 metadata 核对，不是哈希或不可变快照；`changed: true` 时来源完整性降为未知，重新核对引用 |

ANSI 以 JSON 转义保留，CRLF 与中文保留，非法 UTF-8 以替换字符显示并记入缺口。拒绝非普通文件、含 NUL 或识别出的 ZIP/Gzip 输入，但不声称覆盖所有二进制格式。工具不自动脱敏。

## 产物与异常

只读诊断默认使用 stdout，不额外落盘。获准保存证据产物时，可用 `--output /approved/output_dir/log-evidence-01.json`，父目录须已存在；仅创建新文件，拒绝覆盖已有文件及其符号链接/硬链接，stdout 返回路径收据。避免用 shell 重定向覆盖输入，因 shell 可能在脚本启动前截断文件。该可选产物不替代或新增必交的 `analysis-fix.md`。

非法参数、缺文件、无权限、非文本或输出创建失败返回 `status: error`、原因及非零退出码；无命中仍为零退出码。读取受限、文件变化与解码替换保留实际观察及局限。Agent 再按[结束取证](failure-evidence-extraction.md#结束取证)决定返回主流程、补读或处理关键缺口。
