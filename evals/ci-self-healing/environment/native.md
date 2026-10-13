---
ai_generated: true
reviewed: false
---

# 本机 iCode 测试环境（默认）

复用机器已安装的 iCode、Python、JDK、Maven、Git 和 rg，不需要 wheel、新镜像、Docker 或 Colima。当前本机后端使用 macOS `sandbox-exec`；不支持的平台或缺少必需能力时停止，不回退到裸跑或 Docker。不设版本号白名单；记录实际版本并检查接口、Skill 工具、搜索、shell、Git 和 CLI 能力。

当前默认命令为 `icode`。为兼容既有批次，配置键 `chrys` 继续表示显式选择的原生 CLI 路径，应填入实际 `icode` 路径；不要据字段名启动旧命令。已核对的 iCode 仍使用 `chrys` Python 包、`.chrys` 配置目录及 `CHRYS_*` 环境变量，这些不是旧版安装残留。切换 CLI/Python 后必须新建配置、冻结与批次，不修改历史收据；后续版本仍以预检结果决定兼容性。

## 1. 无模型环境准备

使用所选 CLI 所属 Python，避免系统 Python 缺少依赖。`--output` 必须是仓库 `evals/ci-self-healing/runs/` 下尚不存在的绝对规范路径；每次验证新建目录，历史结果不覆盖。

```bash
EVAL_PYTHON=/absolute/path/to/icode-tui/bin/python
EVAL_ENV=/absolute/repo/evals/ci-self-healing/runs/native-environment

"$EVAL_PYTHON" -B evals/ci-self-healing/environment/native_setup.py \
  --output "$EVAL_ENV" --cli /absolute/path/to/icode --warm-cache --build-smoke \
  --model-profile /absolute/home/.chrys/models/selected-glm52.yaml \
  --endpoint-host approved-gateway.example.invalid
```

- 未提供 `--cli` 时只在 PATH 中查找 `icode`，找不到则停止，不自动回退到 `chrys`。Maven/rg 自动定位；JDK 通过 `/usr/libexec/java_home` 定位，也可用 `--java-home` 指定。JDK 与 Maven 环境只设置到子进程，不修改全局配置。
- Git 可由 `--git /absolute/path/to/git` 指定；如果 PATH 命中 macOS `/usr/bin/git` 分发入口且 Command Line Tools 的实际 Git 存在，优先选后者。记录实际路径、版本及二进制摘要，将该目录加入子进程 PATH，不改系统 `xcode-select` 或全局配置，也不开放整个 Xcode 目录。预检通过真实 Chrys shell 在临时合成仓库执行 `status/diff/log/rev-parse`，确认包含原有修改和未跟踪文件的仓库在检查前后不变；关闭用户／系统 Git 配置、交互认证和可选索引刷新。预检中的初始化与合成提交只属于临时测试仓库，不是被测项目的提交权限。
- `--warm-cache` 仅用受控合成 POM 从公开 Maven 仓库下载依赖到本次 `repository/`。使用空的用户／全局 Maven settings，不读取 `~/.m2/settings.xml`；不复制整个用户 Maven 仓库。
- 已有专用缓存时，以 `--repository /absolute/.../runs/old-environment/repository` 替代 `--warm-cache`。验证和 Agent 各用自己的缓存副本，不回写共享缓存；不允许与 warm-cache 同时使用。
- `--build-smoke` 自动遍历全部三个案例，复现原始编译失败，并对两个预先构造的合成修复变体进行 Maven、javac 和行为 Oracle 检查。它验证框架，不能计为模型成功自愈。
- `--model-profile` 与 `--endpoint-host` 用于生成可供后续真实运行的配置；此命令不启动模型网关、不读取密钥或调用模型。省略 model-profile 可只测本机环境，但生成的 runtime 会有明确占位路径，不能直接进行真实回归。

输出 `runtime.json`、`result.json`、沙箱预检日志和各案例编译回执。`doctor` 验证原生 Skill 读取、实际搜索、私有 Agent profile 发现、越界读写／子进程读取被拒绝、Skill 不可写、仅指定回环端口可连接。它还调用已安装 Chrys 的真实 shell 工具，检查标准输出／错误、退出码 0／7、工作目录和临时文件读写清理；仅执行 `subprocess` 或检查工具存在不算 shell 预检通过。另用合成终端验证本进程能创建 PTY，而独立宿主 PTY 的只读、只写和读写打开均被拒绝；故意禁用 PTY 的反向探针必须让 shell 检查失败。这些检查在任何付费模型执行前完成，每个试次也重新检查 shell 工具。预检不是对任意恶意进程的完整安全证明。

## 2. 统一整套入口

将 `batch.example.json` 复制为本机配置，保持 `backend: native`，填入上一步 runtime、所选 iCode/Python、显式选定的 GLM 5.3 评审 profile 和获准 endpoint。配置只存路径与非秘密参数，不存凭据。无需 `docker_context`、容器网络或宿主 `CHRYS_EVAL_API_KEY`。

```bash
"$EVAL_PYTHON" -B evals/ci-self-healing/eval.py doctor --runtime "$EVAL_ENV/runtime.json"
"$EVAL_PYTHON" -B evals/ci-self-healing/eval.py test --config /absolute/path/batch.local.json --plan
"$EVAL_PYTHON" -B evals/ci-self-healing/eval.py test --config /absolute/path/batch.local.json --execute-live
```

最后一条才启动付费模型。单次命令完成全部用例的 GLM 5.2 自愈、确定性检查、GLM 5.3 独立评审和汇总。`--preset regression` 为每例三次；续跑、预算、结果含义与上级说明一致。旧 Docker 现场使用容器路径，不能原地切到本机后端，应新建 suite。

### 失败、预算与续跑

- 语义判定失败仍记 `failed`，其他用例继续；模型的失败评审不会触发环境停止。
- 单例时间／输出预算耗尽记 `incomplete`。只有收到本次进程组回收确认、自愈网关回收确认（自愈适用）、隔离及最终现场快照后，才继续下一个未运行用例；不对该试次追加评审、自动重跑或用后续结果覆盖它。
- 鉴权拒绝、配额限制、预检失败等停止整批；未知退出、证据问题或清理无法确认也保守停止，但不声称已证明共享环境故障。原因记录在 journal 与汇总的 `execution_failures` 中，包含 `code/scope/batch_action`。
- 旧配置的 `stop_after_transport_errors` 仅兼容解析，已由上述原因分流取代，不再通过任意非零退出累计决定是否继续；新配置省略它，`--plan` 会显示新策略。
- 显式续跑校验已完成证据，不重放已超时试次，也不为其补发评审。未解决的共享／未知／安全问题阻止继续；用户中断后的显式续跑可处理剩余独立用例。框架、工具或模型配置变更必须新建批次，旧结果保持不变。

本机模型进程在原执行时限内预留最多 5 秒退出宽限：先发中断信号、继续收集有限输出，硬截止后终止自有进程组并核对回收。中断后进程返回 0 也仍是超时，不转为成功；不增加原有模型时间／token／调用预算，不发送额外模型请求让它补写报告。回收确认仅覆盖自有进程组，不证明恶意脱组进程已被清理。

### 沿用模型配置与响应完整性

自愈不额外设置单轮 token 上限。试次内的隔离模型配置原样沿用所选 profile 的 `max_output_tokens`，不再与框架默认值取较小者；不修改全局 profile、模型身份或独立评审额度。这里仍受模型配置和服务自身限制，不表示无限生成，也不保证十分钟内必定交付。runtime 不再接受 `repair_max_output_tokens`：旧配置带有该字段时明确报错，不静默忽略或改变旧批次含义；新执行须使用新配置和新批次。

本地网关核对实际 Responses 请求中的 `max_output_tokens`，必须与所选 profile 一致，否则在转发前拒绝，不静默补参数。`run.json` 的 `generation_budget` 使用 `profile-generation-v1`，记录相等的 profile 上限和实际配置值，不再记录框架指定额度；实际请求值及终止状态记录在绑定的网关事件中，纳入运行诊断及收据校验。只记录白名单状态、原因和可取得的输出 token 数，不记录请求正文、响应正文或推理内容；流式块数不是 token 数。

`response.incomplete`（含达到生成额度）、`response.failed`、缺失／矛盾的终止元数据或观察到输出量超限，都不能成为正常完成；进程退出 0、报告自称成功或后续状态正常也不能豁免。观察到上述情况后，网关拒绝后续模型请求，不新增自动重试／续跑，不改写响应或补齐截断工具参数。仅有请求上限与正常终止状态不证明供应商内部严格执行了计算额度；输出 token 数未返回时明确保留未知。

该规则是配置一致性及事后验收约束，不是工具执行前安全拦截器，不能证明此前没有发生过工具操作。已有改动以现场快照和轨迹为准，不由框架自动恢复或代写 `analysis-fix.md`。正常退出但生成证据不合格时，本轮保守停止批次；原有超时取消优先保留超时及清理结果。旧 `generation-budget-v1` 收据仍按当时记录的额度校验；更早未采集此类证据的试次不补造，也不改判旧结果。

宿主的“预留收尾时间”仍是提示，不是强制切换报告阶段。模型原有输出上限与硬超时也不保证 Agent 及时停止；是否正确补证、停止并交付仍由真实三用例回归和独立评审验证。

宿主生成 `runtime-diagnostics.json`，明确 `author: harness`、`agent_report: false`，记录进程结果、退出动作、可取得的改动路径、结构化网关状态和耗时统计；它不放入 Agent 的交付目录，也不代替 `analysis-fix.md`。统计仅提取原生轨迹的时间、流式块、停顿、输入／输出／推理 token 数及公开工具耗时，不导出提示词、工具正文或推理文本。未结束的请求不补造 token 数；统计缺口不改变语义评分。回执绑定诊断文件摘要，运行后不回写历史证据。

### 取消与网关错误归因

网关将上游 HTTP／传输错误、下游写入断连及本地策略／内部错误分开记录。事件包含试次、网关实例、请求编号、单调时钟域与时间、操作位置及白名单异常类型；不记录异常正文、凭据、提示词或模型推理。HTTP 状态只记录真实观察，不再把程序异常转换成 502。宿主 `process.json` 另存终止原因、PID／进程组和信号发送结果；本机网关通过宿主绑定共享可比较的时钟域，不能跨宿主或旧试次直接比较时间。

只有本次宿主因超时／输出超限成功发出取消信号、请求在取消前已启动、明确的下游写入断连发生在取消后，且进程组、网关、隔离和现场保存均确认完整时，才按单例预算耗尽继续。没有终止事件的在途请求也只能按同样的宿主取消依据视为已中止，不视为响应正常完成。上游错误即使发生在取消之后也不忽略；本地策略拒绝不冒称供应商鉴权或配额故障。无法归因、记录截断／缺失、旧版仅有布尔错误标记或清理未确认，均停止批次。耗时统计缺口与上述归因缺口不同：前者不代表模型修法错误，后者不能用于放行后续付费运行。

### 无模型框架回归

```bash
"$EVAL_PYTHON" -B -m unittest discover -s evals/ci-self-healing/tests -q
"$EVAL_PYTHON" -B -m unittest discover -s evals/ci-self-healing/environment -p 'test_environment.py' -q
```

组合测试使用真实回环 HTTP 网关与受宿主管理的测试客户端，上游通过内存模拟，不调用模型、不读取模型凭据或访问外部服务。环境须允许监听 `127.0.0.1` 和管理本次子进程组；权限不足会测试失败，不静默跳过。测试在客户端已消费首个数据片段后等待宿主取消，确认客户端退出后才释放后续上游数据，检查取消归因、现场快照以及整批继续／不重放行为；也覆盖取消后发生真实上游读取错误时仍停止。它证明框架行为，不计为 Skill 语义正确或真实模型端到端通过。

## 3. 执行与隔离

每个试次有独立 `visible/` 现场、只读 `runtime/skills/ci-self-healing/` 快照、Maven 缓存副本、临时目录和 `native-session/`。现场与构建命令使用本机绝对路径，不创建 `/trial` 或 `/skills` 全局目录，也不全局安装 Skill。维护文档和测试答案不进入 Skill 快照。

自愈由观察器通过所选 iCode Python 调用原生 `icode run` 的 CLI 入口，未另写模型执行器；观察记录保留实际 CLI 路径。为避免修改全局 Agent/profile，`host_worker.py` 在导入 CLI 前仅为该进程指定私有配置目录；这不是公开配置目录参数，依赖的接口会先检查。不会改写安装文件，也不修改 `HOME`。私有配置目录中的运行状态和新生成的会话密钥仅属于本次试次，不使用宿主全局凭据。

真实模型 profile 由沙箱外的受控网关进程按指定路径读取；模型仅支持当前套件指定的 GLM 5.2。真实 API key 不进入自愈进程环境、文件或命令参数。自愈进程只持本次临时本地令牌，仅能连接网关的回环端口；网关只转发获准模型的 Responses 请求，拒绝重定向和额外媒体／托管工具。网关最多 256 个请求，不等于 token/费用上限。试次结束／超时后回收本次网关进程组。

自愈可写范围包括案例现场及本次必要运行目录；Skill、观察器与任务输入只读，其他试次、评分答案、宿主模型 profile 和历史会话不可读。shell/Maven 子进程继承同一沙箱。编译验证在独立无网络副本中运行，不改动候选提交现场。父级持有评分文件和公开事件日志，原生会话在宿主本次目录持续落盘。

Chrys 的 Unix shell 工具需要 PTY。只有自愈及其工具预检策略启用 PTY：允许 `/dev/ptmx`，并通过 `com.apple.sandbox.pty` 系统扩展限制对本进程新建从设备的访问；不整体开放 `/dev` 或任意 `/dev/ttys*`。独立编译验证不启用 PTY。若出现 `out of pty devices`，应先区分真实设备耗尽与沙箱拒绝；环境检查失败不计为 Skill 修复失败，也不自动重试付费会话。修复框架后新建批次，保留旧记录，不覆盖或复用已中断试次。

沙箱默认仍允许必要系统行为，允许读取指定工具运行库与路径祖先目录；不承诺隐藏所有文件名、抵御任意恶意本机程序或不可绕过的系统调用审计。普通子进程组随超时回收；主动脱离进程组等恶意行为不属于初版证明范围。真实模型轨迹仍须独立检查，不能把 doctor 或构建冒烟当成完整端到端通过。
