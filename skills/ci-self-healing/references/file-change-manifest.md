# 变更文件清单

本次传入 `output_dir` 时，结束交付除 `analysis-fix.md` 外，必须在同一目录生成 `file_change.json`，供外部系统读取并生成 Patch。仅诊断、停止、失败和案例建议分支同样适用；无代码修改时仍输出空数组。未提供 `output_dir` 时不自行落盘此文件。

## 文件格式

使用 UTF-8 标准 JSON，无注释或尾逗号，数组键名固定为 **`changes`**：

```json
{
  "version": "0.1",
  "changes": [
    {
      "file": "src/main/xxx/xxx.java",
      "op": "modify",
      "repo": "example-repo",
      "desc": "补充缺失的导入，恢复已有接口调用"
    }
  ]
}
```

顶层仅有 `version`、`changes`；条目仅有 `file`、`op`、`repo`、`desc`，所有字段必填：

| 字段 | 约定 |
| --- | --- |
| `version` | 固定字符串 `"0.1"`，不是数字 |
| `changes` | 文件变更条目数组；无代码修改时为 `[]`，不省略、不填 `null` |
| `file` | 相对该文件所属 Git root 的 POSIX 文件路径，用 `/` 分隔；无前导 `/`、仓库前缀、反斜杠、空路径段、`.` 或 `..` 路径段；删除文件也保留其原相对路径 |
| `op` | 仅 `modify`、`create`、`delete` |
| `repo` | 该文件所属实际 Git root 的 basename（最后一级目录名），与外层调度器的仓库匹配键一致；不是项目显示名、远端仓库名、目录绝对路径或 `owner/repo` |
| `desc` | 非空修改说明，描述实际变更及作用，不填写凭据或敏感原文 |

无代码修改时的完整文件：

```json
{"version": "0.1", "changes": []}
```

## 从最终交付状态生成

1. 复用已确认的仓库身份、实际 Git root、修改归属和交付基线。`repo` 固定取该 Git root 的 basename，`file` 相对同一 Git root 计算；必要时只读确认 `git rev-parse --show-toplevel` 的结果。`work_dir` 不一定是 Git root，不能直接取其 basename；也不能用 `build_info` 等仓库映射中的逻辑名称、远端名称或构建项目名替代。多仓按各自 Git root 分别计算；worktree 使用当前工作树的 Git root，不取 `.git` 或公共 Git 元数据目录。无法确认 Git root，或多个 Git root 的 basename 相同、调度器无法唯一匹配时，报告交付缺口，不自造别名或添加路径前缀。
2. 核对本次及明确继承、属于本次交付范围的源码、测试、构建配置等净变更。保护用户/Job 原有修改，不能直接把全量 `git status` 当作本次清单；同文件混有原有修改时在报告说明，文件级清单不能分离修改片段。Patch 提取基线与介入时的修改归属判断分别核对，不能互相替代。
3. `op` 按对应仓库已确认的 Patch 提取基线与最终状态判定：原有文件改变为 `modify`，新增为 `create`，删除为 `delete`；重命名/移动拆为旧路径 `delete` 与新路径 `create`。同路径多次编辑合并为一项，恢复到交付基线或先创建后删除且无净差异的不列入。每个 `(repo, file)` 唯一，建议按仓库名、路径排序。
4. 排除报告、`file_change.json` 本身、取证日志、校验清单和构建临时副产物；实际作为候选交付的源码或配置按归属判断，不能仅凭目录名排除。已有恢复/清理完成或其缺口已查明后，按最终状态填写；停止或验证失败不等于无修改，仍留下的未恢复源码改动须在条目说明和报告中如实披露，不能用空数组隐藏。

例如项目显示名为 `routing-feature`、实际 Git root 为 `/job/scene/sourcecode`，修改 `/job/scene/sourcecode/src/main/java/example/Feature.java` 时，必须填写 `repo: "sourcecode"` 和 `file: "src/main/java/example/Feature.java"`。即使从其 `src/` 子目录开始工作，取值也不变。

仅诊断且没有继承的交付修改时，直接输出空数组，无需为了生成清单建立修复身份、读取修复专用合同或开始构建。无法确认条目范围、Git root、其 basename 或操作时，不编造条目或以空数组代替未知，按下节报告交付缺口。

## 写入、检查与交接

遵循[输出准备](output-delivery.md)的路径解析、授权、同名文件保护及工作空间布局规则。只有本次调用拥有且可安全更新的同名文件才可改写；不能覆盖上次调用或用户文件。文件无法安全生成时说明实际路径、原因和缺口，不静默改名、换目录或声称清单已交付。

生成清单前完成相关源码写入、恢复与清理；最终 JSON 写完后，用宿主指定脚本工具执行 [file_change_check.py](../scripts/file_change_check.py)，并完整只读读回清单。校验与读回须覆盖同一份最终字节，核对收据 `sha256` 与实际文件一致；两项可在写入完成后并行，不与清单写入并行。以下为有序参数：

```json
["--manifest", "/approved/output_dir/file_change.json"]
```

脚本使用 Python 3.9+ 标准库，只读检查 JSON、字段、版本、路径格式、操作和重复条目；返回 `format_status`、文件原始字节的 `sha256` 与问题列表。`valid`/0 表示格式通过，`invalid`/1 表示格式有误，`incomplete`/2 表示文件不可读取或超过 2 MiB 读取上限。脚本不读取仓库、不判断修改归属或实际 Git root，也不证明 `repo` 等于该 root 的 basename、未带仓库前缀、清单无遗漏、`op` 正确或修复成功。

Agent 对照最终差异与报告第 4 节核对条目、操作、仓库及说明，并逐仓确认 `repo == basename(Git root)`，由同一 root 与 `file` 定位实际变更文件；删除项核对其原位置。报告使用项目显示名时同时注明 Git root 与清单的 `repo`，保留对应关系。格式错误先纠正；脚本不可用或无法完成校验时，保留可生成的文件，在报告第 6 节明确校验缺口，不声称完整交付检查通过。最终文件变动须重新检查并读回；源码再变动须重新核对清单，外部接手前保持所列文件与清单一致。

在报告第 6 节和最终交付消息中给出实际清单路径及必要缺口。宿主应在本次调用结束且交付检查完成后接手，不把文件首次出现当作交付完成。清单只描述文件变更，不是修复通过、清理完成或安全提取的许可；外部系统仍须核对报告、仓库/基线及原有交付条件，再按清单生成 Patch。Skill 不因生成清单执行 Patch 提取、commit、push 或其他外部动作。
