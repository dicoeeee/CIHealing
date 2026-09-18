# CI 自愈 Skill

这是一个面向人类使用者的说明文件。它介绍本 Skill 能做什么、如何调用以及结果如何交付；Agent 的正式执行规则以同目录的 [SKILL.md](SKILL.md) 为准。

## 解决什么问题

本 Skill 用于处理已经发生、且有望通过代码或构建配置修复的确定性 CI 失败。它会：

1. 保存并绑定原始失败、仓库、基线和构建环境；
2. 获取与失败相关的 MR、提交历史、需求/设计材料、测试和实际调用上下文；
3. 判断目标行为、根因和可行的修复位置；
4. 在修改前记录修复准入；
5. 修改工作空间并执行分层验证；
6. 输出诊断与修复报告，或在证据不足时停止并说明需要补充什么。

“编译通过”只是验证信号，不等于已经证明修复符合开发者意图。

## 适用范围

- Java/Maven 源码、测试源码、生成代码和编译配置问题；
- C/C++ 预处理、编译、模板、符号、链接和 ABI 问题；
- 依赖声明、解析、获取、制品一致性和实际消费问题；
- 可以由 GitHub Actions、GitLab CI 或企业 CI 适配器提供证据的具体流水线失败。

以下事项不属于本 Skill 的默认范围：普通流水线编写、已确认的 Flaky/基础设施恢复、合并、部署、生产事故处置，以及没有证据支持的盲目改动。

## 调用方式

Skill 支持两个可选位置参数，顺序为 work_dir、output_dir：

~~~text
/ci-self-healing "/path/to/failure-scene" "/path/to/healing-output"
~~~

也可以使用宿主提供的命名输入：

~~~text
使用 $ci-self-healing 修复本次构建失败。
work_dir: /path/to/failure-scene
output_dir: /path/to/healing-output
~~~

参数含义：

| 参数 | 作用 |
| --- | --- |
| work_dir | 失败现场目录，可包含日志、源码/工作空间和构建上下文 |
| output_dir | 可选的报告输出目录；有效时生成 analysis-fix.md |

同时提供两个参数会进入企业 CI Job 失败现场场景；只提供 output_dir 不会改变验证模式。宿主不支持位置参数扩展时，使用命名输入或宿主自己的结构化参数接口。

## 两种验证模式

| 模式 | 适用场景 | 外部动作 |
| --- | --- | --- |
| local_verify | 当前本地仓库或远端 Runner 的 Job 工作空间内修改并验证 | CI Job 工作空间不 commit/push；后续脚本负责提取 Patch |
| remote_verify | 明确授权后，将专用候选 ref 发布到远端并触发独立流水线验证 | 只能使用专用候选命名空间；不写默认/受保护分支，不合并、部署或发布 |

运行在远端 Runner 本身不会自动切换到 remote_verify。

## 关键决策原则

- 用户意图和适用契约优先于“最小改动”以及“尽快变绿”。
- 调用方、被调用方、重复类型、适配层或依赖版本存在相反且都有依据的语义方向时，必须继续取证或停止，不能凭提交顺序、改动行数或编译通过自行选择。
- 绿色构建不能替代目标行为证据，也不能覆盖未处理的反证。
- 没有交互或移交通道时，输出 inconclusive 及缺口，不等待不存在的人工响应。
- Skill 不自动生成完整 Patch；在 CI Job 场景中保留稳定工作空间供后续脚本提取。

## 报告

最终报告标题为“CI 自愈诊断与修复报告”，固定包含：

1. 处理结论；
2. 失败与根因；
3. 目标行为与修复依据；
4. 实际修改；
5. 验证结果；
6. 剩余问题与交接。

提供有效的 output_dir 时，报告写入：

~~~text
<output_dir>/analysis-fix.md
~~~

没有 output_dir 时，直接在宿主消息中输出完整文字报告。报告交付失败不会被伪装成成功，也不会静默改名或换目录。

## 目录说明

- [SKILL.md](SKILL.md)：Agent 执行入口和硬边界。
- [references/diagnosis-and-repair.md](references/diagnosis-and-repair.md)：失败合同、上下文取证、意图判断和停止规则。
- [references/repair-and-verification.md](references/repair-and-verification.md)：准入、候选、分层验证和结果判定。
- [references/execution-modes.md](references/execution-modes.md)：本地/远端模式、工作空间保护和协作交付。
- [references/result-report.md](references/result-report.md)：文字报告结构和 analysis-fix.md 输出约定。
- [references/provider-contract.md](references/provider-contract.md)：CI Provider 能力与证据边界。
- [references/java-maven-compilation.md](references/java-maven-compilation.md)：Java/Maven 专项指导。
- [references/c-cpp-compilation.md](references/c-cpp-compilation.md)：C/C++ 专项指导。
- [references/dependency-diagnosis-and-repair.md](references/dependency-diagnosis-and-repair.md)：依赖问题专项指导。
- [references/ci-job-failure-directory.md](references/ci-job-failure-directory.md)：企业 CI Job 失败现场目录约定。

## 重要提醒

这是仓库内的 Skill，不需要全局安装。README 只服务于人类理解和接入；修改执行规则时，应更新 [SKILL.md](SKILL.md) 及对应 reference，并保持参数、报告和中文归档同步。
