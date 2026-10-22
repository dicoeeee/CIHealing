---
ai_generated: true
reviewed: false
---

# CI 自愈 Skill 变更回归测试（初版）

这是维护者使用的独立测试器，不属于 Skill 的运行上下文，不需要从 SKILL.md 或 references 引用它。没有修改 Skill 参数、状态或执行规则，也不安装全局 Skill。

整套入口是 `eval.py test`：一次命令自动完成新建现场、GLM 5.2 / Chrys 自愈执行、确定性检查、**原生 `chrys run` / GLM 5.3 独立评审**和汇总。Chrys 不设版本号白名单，按实际接口能力预检并记录版本。默认跑全部已注册用例，不需要人工传递 trial ID 或逐个拼接步骤。旧分阶段 HTTP / GPT-6 Sol 适配器和轻量原生收集器保留作调试与历史复核，不会成为整套入口的回退路径。

基础套件目前有三个 Java/Maven 参数不匹配案例：保持原契约、扩展契约、证据不足应停止。框架可重复执行不等于已覆盖所有 CI 问题；Profile、跨仓、C/C++ 和交互/移交案例尚未实现。

## 怎么使用

默认整套入口使用 **macOS + 本机 Chrys/JDK/Maven/rg**，无需 Docker、镜像或 wheel。自愈和评审分别使用 macOS 沙箱，编译验证在无网络的候选副本中完成；不支持的平台或缺少必需能力时拒绝执行，不退回无隔离运行。使用已安装 Chrys 的 Python，需含 PyYAML、jsonschema；Chrys 本身的 Python 要求由所选安装包决定。离线单元测试支持 Python 3.9+、不调用模型或网络；有 JDK 时额外检查真实编译。旧 Docker 后端仅供显式兼容使用。

```bash
python3 -B -m unittest discover -s evals/ci-self-healing/tests -v
```

### 一次配置，反复执行

先按 [本机环境说明](environment/native.md) 准备专用 Maven 缓存和完成无模型预检，再基于 `batch.example.json` 创建本机 JSON 配置，保持 `backend: native`。填写 runtime、已安装 Chrys/Python、已选 GLM 5.3 profile、获准网关和输出根目录；相对路径按配置文件所在目录解析。配置中不写 Key。原生评审只读指定 profile，要求现有 `~/.chrys/trajectory-hmac.key` 已存在；不安装模型、不修改全局配置或创建全局密钥。

```bash
EVAL_PYTHON=/absolute/path/to/chrys/bin/python
EVAL_CONFIG=/absolute/path/to/local-batch.json
EVAL_RUNTIME=/absolute/path/to/isolated-repair-runtime.json

# 只显示整套选择和调用预算：不创建现场，不启动 Docker，不调用模型。
"$EVAL_PYTHON" -B evals/ci-self-healing/eval.py test --config "$EVAL_CONFIG" --plan

# 正常入口：全部用例，当前 Skill，每例一次。再次执行自动创建另一套全新现场。
"$EVAL_PYTHON" -B evals/ci-self-healing/eval.py test --config "$EVAL_CONFIG" --execute-live

# 同一批次比较 Git 基线与当前工作区，每例每版本重复三次。
"$EVAL_PYTHON" -B evals/ci-self-healing/eval.py test --config "$EVAL_CONFIG" \
  --mode compare --preset regression --baseline-ref HEAD --execute-live
```

本机后端直接执行上述入口，不使用 `manage.py exec`，也无需 Docker context 或预设的 `CHRYS_EVAL_API_KEY`。自愈网关按试次启动并清理，真实凭据不进入自愈沙箱。只有明确使用旧 Docker 后端才需要其环境包装器；没有跨后端自动回退。框架不自动安装依赖或启动 VM。

| 模式 | 每例重复 | 当前自愈试次 | 最多评审会话 |
| --- | --- | --- | --- |
| single + smoke（默认） | 1 | 3 | 3 |
| single + regression | 3 | 9 | 9 |
| compare + smoke | 1 | 6 | 6 |
| compare + regression | 3 | 18 | 18 |

`--candidate` 可指定另一份 Skill 目录；`compare` 拒绝相同快照，`single` 不要求存在版本差异。`--cases case-001,case-003` 仅用于调试子集，正常测试省略。只给 `test` 或显式 `--plan` 都不会调用模型；必须给 `--execute-live`。每次生成带时间与随机 ID 的目录；现场、Skill 快照、用例合同、评测代码、模型和环境绑定分别留存。

### 三用例并发与自愈时限

批次配置 `max_parallel_trials` 默认 **3**，可显式设为 1 或 2，拒绝超过 3。协调器持有整批独占锁，以最多三个独立试次组成一轮并发执行；同一试次始终按 `run → grade → review` 串行推进。本轮所有在途阶段完成并保存证据后，才生成汇总、派发下一轮。较快用例会等待同轮较慢用例，避免汇总读取尚未写完的证据。

会话总预算仍由唯一协调器管理：每个自愈或评审阶段提交给工作线程前，先写不可覆盖的 started 日志并预留一次；grade 不消耗模型会话名额。日志记录 segment、wave、并发上限和全批会话预留序号。即使预留后未能启动，名额也不退回；中断、未知结果和已失败的阶段不自动重放。三个单次用例最多仍为 **3 次自愈 + 3 次评审**，并发不会扩大 `max_agent_invocations`。

工作线程继承同一个批次绝对截止时间；`max_wall_seconds` 按整批墙钟累计，不按线程数量倍增。清理使用自己的有界收尾上下文，不重置其他用例的截止时间。共享环境/模型身份变化、鉴权/限流错误、证据不完整和清理不确定仍停止整批：停止派发，通知在途进程组取消，等待各自网关及归属资源清理、回执落盘，再结束段记账。已启动的会话仍可能产生费用，取消不退款、不回退模型。单例有确认清理证据的超时仍按原合同处理，允许其他独立用例继续。

每个试次继续独占源码现场、Maven 缓存副本、临时配置、网关端口/临时令牌、会话与审计目录；并发不扩大沙箱读写权限。共享运行/评审绑定通过锁串行创建并核对同一身份；差异不会被覆盖。评审仍使用原有独立证据包、模型身份检查和清理判据。

GLM 5.2 自愈默认时限改为 **900 秒**；GLM 5.3 评审仍为 900 秒，工具数、token/profile 限制及批次会话/墙钟预算保持原值。`runtime.example.json`、环境准备器与新冻结配置均使用该自愈默认值；显式配置的其他时限仍按配置执行。`freeze.py` 默认生成并发数 3、自愈 900 秒的新快照，可用 `--max-parallel-trials 1` 或 `--repair-timeout-seconds N` 显式覆盖。历史冻结、运行、报告与回执不修改；旧的显式 600 秒配置不会被加载器静默改写，需要创建新的配置或冻结版本。

### 结果、续跑与预算

标准输出和 `summary.json` 提供机器可读结论，`summary.md` 提供用例清单；按版本、用例和重复次数汇总。`compare` 另给 after 减 before 的计数差，不把小样本差异宣称为统计显著改进。

每次运行目录默认仅当前用户可访问，包含：

```text
run-<time>-<id>/
  batch.json                         # 冻结的配置、计划、评测器摘要
  environment-binding.json           # 实际环境和模型绑定
  summary.json / summary.md           # 可重新生成的汇总
  suite/skills/                      # 本轮 Skill 快照
  suite/trials/<id>/                  # 独立现场、run/grade/review.json、公开执行轨迹
  reviews/<id>/                      # 冻结的评审输入、原生进程回执、session/trajectory
  journal/<id>/                      # 每阶段的 started/finished 及产物摘要
  segments/                          # 每次执行/续跑的时间记账
```

定位失败先看对应 trial 的 `grade.json`（确定性失败/缺口），再看 `review.json` 和 `reviews/<id>/assessment.json`（评审与记录缺口）；阶段没有完成则看 journal 的错误记录。不要直接修改这些文件把状态改成通过。

`assessment.json` 只记录公开输出和 session 的前置检查，尚未读取 exchange 轨迹，因此其中保留的 `formal_evaluation_status` / `call_record_gaps` 不是完整批次总评。新增轨迹检查的最终结论在 `review.json` 的 `status` 与 `model_receipt`，整套结论在 `summary.json`。这些分层记录不互相覆盖。

- `passed`：确定性与完整语义评审均通过；`failed`：已完成检查得到失败证据。
- `incomplete`：超时、轨迹/评审缺失、无效输出或执行结果不确定；`not_run`：尚未启动。
- 退出码 `0`：全通过；`1`：全部完成但有失败；`2`：存在未完成、未运行或批次被阻断。模型 pass 不能覆盖确定性失败。

```bash
"$EVAL_PYTHON" -B evals/ci-self-healing/eval.py test --config "$EVAL_CONFIG" \
  --resume /absolute/path/to/existing-run --execute-live
```

续跑不接受重新选版本/用例。已完成阶段先核验摘要再跳过，已完成自愈但尚未评分时直接继续评分；已报错、中断或结果未知的阶段绝不自动再次调用模型。其余尚未开始的用例可以继续。整个批次已无可执行阶段时，只复核证据和汇总，无需 Docker/模型在线。若协调进程被强杀、段结束记录缺失，拒绝续发付费调用，先人工核对遗留进程/容器，再建立新批次；不猜测旧调用是否扣费。修改配置、评测代码、用例或环境后需新建批次，保留旧证据。

`max_agent_invocations` 限制自愈和评审的**启动次数之和**，开始前保守预留一次；不是 LLM 请求/token/费用上限。自愈内部有多轮请求，原生评审 SDK 也可能有内部重试；这些不等于额外 Agent 启动。`max_wall_seconds` 跨续跑累计执行墙钟，单次调用同时受自身时限限制；到限停止新工作，另留有界时间回收本次容器/进程。一次 Skill 行为失败不会阻断其他用例；执行异常按既有 `typed-v1` 故障分类决定继续或停止，不按非零退出码猜测共享故障。旧 `stop_after_transport_errors` 字段仅兼容解析，不再控制分类。语义格式错误不自动修补、重试或提高预算。

### 原生评审的验收边界

整套入口先记录实际版本，无网络核验 headless CLI 所需选项、内置 QA、轨迹写入接口和沙箱，再用原生 CLI 运行冻结的完整评审输入。评审配置无需填写 `version`；旧配置中的该字段仅为迁移兼容而接受，不作为版本限制，回执以实际 CLI 版本为准。同一批次仍绑定实际环境，升级后新建批次，不能混用版本产生的证据。评审器通过证据包取证，沙箱不允许读取原修复工作区、其他用户项目、其他 profile 或历史会话；允许读取必要系统/Python 文件及指定模型配置，写入仅限本次会话、临时目录和现有轨迹密钥的精确锁文件。宿主不切换 HOME，不改模型配置。在线网络规则限 TCP 443 和 DNS IPC，**不是网关域名白名单**；指定 endpoint 和会话核验不证明阻断了所有其他 HTTPS 目标，也不继承旧 HTTP 适配器的禁止重定向保证。

验收同时检查公开输出的 Schema/证据/时序、进程完成、原生 session 元数据与冻结输入一致，以及 `trajectory/events.jsonl`：单一新会话/覆盖区间、连续序号、完整关闭、恰好一次成对的主 Agent 模型 exchange，请求/响应模型均为 GLM 5.3。显式 stop 可用于完成判定；Responses 正常完成若省略 finish_reason，则必须额外核对 accepted validation、单次且无函数调用的成功 cycle、completed run/turn，以及各开始/结束记录的 ID、父子关系和顺序。缺省值保留为 null，不补写 stop；这证明原生客户端正常完成，不冒充已采集服务端 response.completed 原始事件。轨迹格式不兼容或证据缺失仍为 incomplete；不限制版本不等于保证任意版本均兼容。

工具、子 Agent、压缩、重试事件、覆盖缺口、模型不匹配或缺失记录都不算通过。内置 QA 本身仍带读取类工具；“不使用工具”是提示要求与事后验收条件，不等于工具从 CLI 中被移除。当前只接入本仓合成材料，不适合作为运行任意恶意证据的安全边界。Chrys exchange 记录不能审计 SDK 内部每次 HTTP 重试，也不能独立证明服务商实际后端。

保留原生 session/trajectory 供本机复核，报告只摘取必要回执和摘要，不把隐藏推理作为证据发送。原生会话文件本身可能含模型内部内容，整个 `runs/` 不得提交或上传。评审输出 token 上限沿用所选 profile，并记录该值；不同于旧 HTTP 适配器的 16384 token 参数。墙钟/输入字节/会话数限制不等于精确 token 费用上限。当前实现宁可输出 incomplete，也不根据模型自述猜测调用完整性。

### 如何扩展用例

用例注册入口是 `cases.py` 的 `CASES`：稳定 ID、预期行为类型、对 Agent 可见的 MR 契约。现有 `materialize()` 生成独立现场与 Git 历史；`oracle()` 定义不暴露给 Agent 的行为验收。增加当前 Java 模板下的契约变体不需要改批量调度器，默认整套命令自动纳入。改变语言、源码结构或判定机制时，需要扩展 fixture/build-probe/grade 适配层及其离线测试，不能只加一个分类名称就宣称支持。

每个新用例应同时包含：可复现的真实失败、足以决定修法或应停止的证据、允许/禁止变更范围、独立行为断言、报告与副产物检查。验收不比较固定 Patch；增加已知正确、错误及信息不足的测试变体，防止评分器奖励“一律修绿”或“一律 stop”。用例定义和实现摘要被冻结，续跑时变更即拒绝混用。

## 分阶段与历史调试入口

以下命令用于定位框架问题或复核旧 suite。日常回归使用上面的 `test`，不要人工串联这些命令代替整套运行。

```bash
python3 -B evals/ci-self-healing/eval.py prepare \
  --baseline-ref HEAD --candidate skills/ci-self-healing \
  --output /absolute/path/to/new-eval-suite
python3 -B evals/ci-self-healing/eval.py summary --suite /absolute/path/to/new-eval-suite
```

`prepare` 默认比较 Git HEAD 中的 Skill 与当前工作区，包括尚未提交的 Skill 修改；也可以用 `--baseline-dir` 指定旧版本目录。两个快照相同会拒绝准备。`--repeats 1` 可先生成六次冒烟试次。输出目录必须不存在，结果默认不覆盖；失败试次重测时重新 prepare，不改写旧证据。

新 suite 默认 `--reviewer glm-5.3`；可显式指定 `--reviewer gpt-6-sol` 保留旧适配器。评审器在准备时写入 suite，后续不能静默换模型；需要转换时重新 prepare，不修改旧记录。

`suite.json` 保存私有的新旧版本映射、案例答案、快照摘要及随机顺序。每个试次用不含方向信息的 ID，模型只看到自己的 `visible/` 和只读 Skill 副本，不能访问整个 suite、其他试次、评分器或行为 Oracle。

## 旧 Docker 分阶段运行的前置条件

本节只说明显式选择旧 Docker 后端的兼容方式；默认本机路径见前文，不需要本节镜像或服务。两种后端均不自动切换隔离方式。测试器不拉取镜像、不安装工具、不启动 Docker 服务、不修改现有 Chrys/Codex 配置。本节分阶段 `review` 的 GLM 评审端是受信 HTTP 客户端；整套 `test` 则使用上文原生 Chrys 沙箱评审，二者不混用。

需要显式准备本地环境时，可按 [隔离环境说明](environment/README.md) 使用独立辅助脚本；它与 `eval.py run` 分开执行。环境冒烟不等于真实自愈验证，也不等于语义评审完成。

自行准备并审核固定 digest 的 Linux 镜像，包含：

- 显式选择的 Chrys 安装包（包含其 headless 依赖），满足该包要求的 Python、Git、rg、JDK 17+、Maven；没有固定 Chrys 版本要求。
- 容器普通用户的 home 为 `/home/eval`；可按宿主 UID/GID 运行。容器根文件系统只读，`/tmp` 和 `/home/eval/.chrys` 是限额 tmpfs。
- `/opt/maven-repository` 含本案例离线构建所需完整缓存：resources-plugin 3.4.0、compiler-plugin 3.14.0 及其依赖。没有在线下载回退。
- 正确的 Java/Maven 环境配置。镜像中不带历史会话、私人记忆、其他仓库、真实 CI/Git 凭据或其他隐含模型配置。

准备专用 **internal Docker 网络**，通过受控网关/代理仅提供模型服务；测试容器自身不能直接访问互联网。默认 bridge、host 网络及非 internal 网络均被拒绝。测试器只核查 Docker 的 internal 属性，**不能证明网关的域名白名单或认证策略**，这部分需要部署者核查。模型网关不可用时记录执行不完整，不记 Skill 失败。

从 `runtime.docker.example.json` 建立本地 runtime 配置，填入真实镜像 digest、internal 网络、独立的 Chrys 配置目录、Agent profile 和实际请求模型 ID。新建 Docker 现场时给 `prepare --backend docker`；整套配置显式给 `backend: docker` 与 `docker_context`。默认的 `runtime.example.json` 属于本机后端，两者不能混用。示例占位符不能运行。

独立 Chrys 配置目录应仅含必要的设置、`EvalCode` Agent profile 和名为 `huoshan-glm5.2` 的模型 profile，不要复制整个 `~/.chrys`。不要启用其他模型路由、子 Agent、无关 MCP、hooks 或记忆；测试任务中的无交互约定与生产场景对齐。模型凭据在 profile 中引用 `{{CHRYS_EVAL_API_KEY}}`（Chrys 的环境变量模板语法），由外部秘密注入设施提供同名环境变量；不要把凭据写进 runtime JSON、案例或仓库。配置复制到容器 tmpfs，原目录只读挂载。使用本目录的隔离网关时，此变量仅为本地网关令牌，不是真实上游 Key。

```bash
python3 -B evals/ci-self-healing/eval.py doctor --runtime /outside/runtime.json
python3 -B evals/ci-self-healing/eval.py run \
  --suite /absolute/path/to/new-eval-suite --runtime /outside/runtime.json \
  --trial trial-XXXXXXXXXXXX --execute-live
python3 -B evals/ci-self-healing/eval.py grade \
  --suite /absolute/path/to/new-eval-suite --runtime /outside/runtime.json \
  --trial trial-XXXXXXXXXXXX
/absolute/path/to/chrys/bin/python -B evals/ci-self-healing/eval.py review \
  --suite /absolute/path/to/new-eval-suite --trial trial-XXXXXXXXXXXX \
  --review-profile /absolute/path/to/huoshan-glm5.3-profile.yaml \
  --max-packet-bytes 524288 --max-output-tokens 16384 --timeout 600 --execute-live
python3 -B evals/ci-self-healing/eval.py summary --suite /absolute/path/to/new-eval-suite
```

`--trial` 用 prepare 生成的真实 ID；省略表示全部试次。这些是调试命令，整套入口无需人工传递 ID。`run` 和 `review` 都必须明确给 `--execute-live` 才会调用模型。18 次执行和最多 18 次评审不是一次模型调用；时间/工具调用限额不等于精确费用上限，token 费用需结合服务端账单核对。

GLM 评审使用现有 Chrys Python（含 `httpx`、`PyYAML`、`jsonschema`），显式读取名为 **huoshan-glm5.3**、provider 为 `openai`、接口为 `responses` 的非 Flash profile。仅复用其模型连接信息，不启动 Chrys Agent，也不加载历史会话、记忆、其他配置或工具。原始凭据只在可信客户端内存中用于认证，不传给模型、不复制进 suite。每次评审是一条新请求：`tools=[]`、`tool_choice=none`、`store=false`，不传会话或 continuation ID；不重定向、不自动重试或降级。响应声明 `json_schema`，同时在本地再次校验 Schema、证据引用和时序。拒答、截断、工具调用、模型 ID 不匹配和格式不符均记 incomplete。

GLM 评审默认 `--max-output-tokens 16384 --timeout 600`，前者包括推理和公开输出。token 参数允许 1–32768，实际使用值不超过 profile 上限；不会直接采用 profile 中很大的最大值，也不会因截断自动加预算重试。墙钟参数允许 1–1800 秒，与 token 数分别限制；显式增加 token 时也应按实测速度调整时限。worker 到期停止请求并输出 incomplete，父进程另留 5 秒仅用于结束及保存诊断，超出仍强制终止。旧 Sol 默认时限仍为 300 秒，不接受 GLM token 参数。

GLM 评审证据包默认 `--max-packet-bytes 524288`（512 KiB），允许 1–1048576 字节。按完整 packet 的规范化 UTF-8 JSON 计量，不按字符数计算；文件和事件全部保留，不去重、摘要化或截断。stdin 封装上限为 packet 预算加 64 KiB，HTTP 请求上限为该值的两倍，为 rubric、Schema 和 JSON 字符串转义留出空间。三层分别测量，父进程预检后 worker 再检查；发送的就是经过测量的请求字节，摘要与大小写入诊断。超过任何上限均停止，不因提高 packet 上限而取消传输边界。

字节预算不是模型 token 上下文保证。请求显式设置 `truncation=disabled`，不使用压缩或历史裁剪；服务端上下文不足、拒绝请求或输出不完整仍为 incomplete，不自动重试或扩大预算。兼容服务是否真正执行该参数不能仅由 HTTP 200 证明。旧 Sol 路径保留原 256 KiB packet 上限，不接受此 GLM 参数。

`reviewer-binding.json` 固定模型请求 ID、脱敏配置摘要、传输/容量代码、评审规则、Schema 摘要、三层输入容量、请求/有效 token 上限和时限；不包含 Key，正常密钥轮换不改变绑定。已有绑定后修改预算或评审配置须新建 suite，不覆盖旧证据。首次容量预检失败不会新建绑定或开始模型调用；没有绑定和评审输出时，可修正容量后继续评审原有、绑定仍有效的修复证据，无需重跑自愈。成功回执还须匹配父进程预检的完整 packet 和实际 HTTP 请求摘要。

记录响应模型身份及用量，但接口返回的身份不是对服务商实际后端的独立证明。GLM 5.2 与 5.3 来自同系列：这里的独立指上下文、执行权限及证据隔离，不保证错误统计独立；仍须用人工标注样例校准。

传输遵循 profile 的 `stream` 设置，并纳入配置摘要；[流式响应](https://developers.openai.com/api/docs/guides/streaming-responses)只接受完整的 `response.completed`，不会把片段当结论。两种传输都受整体时限及响应字节预算限制。输出 Schema 同时放入接口参数和显式提示；当前兼容链路的短对照曾返回违反 `strict` Schema 的结果，因此标记 `schema_enforcement=local_required`：worker 和评审汇总分别校验，不把服务接受参数当作格式保证。拒绝重复 JSON 字段、NaN、Markdown 围栏和缺失字段，不自动修补输出或修改结论。

`review-execution/stderr.log` 实时保存安全进度；`stdout.log` 保存单个结果回执，成功和失败均有明确 status。诊断包含阶段/耗时、HTTP 状态、终止原因、事件计数、用量及 Schema 失败路径；完整或 incomplete 终止响应中的公开文本最多留存 128 KiB，并明确标记诊断截断，不影响原始响应的严格校验。不保存响应头、原始响应体、隐藏推理或加密推理。`review.json` 会保留 worker 的失败诊断；若父进程被迫终止而回执缺失，明确记为 `worker_receipt_unavailable`，不能猜测失败原因。

主要错误码：`output_token_limit`（推理/输出预算耗尽）、`wall_timeout`（整体超时）、`transport_timeout`（网络读写超时）、`schema_invalid`（结构不符）、`semantic_invalid`（证据或时序等校验失败）。它们均为评审 incomplete，不等于被测 Skill 失败；只有全部校验通过的评审 verdict 才参与汇总，仍不能覆盖确定性失败。

仅旧 Sol suite 需要 Codex CLI 已获准使用 **gpt-6-sol**：全新 `codex exec`、high reasoning、忽略用户 config/rules、read-only sandbox、ephemeral session、`--output-schema`。该路径不接受 `--review-profile`，也不会在访问失败后替换模型。

## 原生 Chrys 评审：最小准备与记录

`review_chrys.py` 只有 `prepare` 和 `collect`，均为离线操作，不启动 Chrys、不读取凭据、不发请求、不修改全局配置或原 suite。与 HTTP/Sol 评审共用输出合同：五个固定评审项各自填写 `id/status/evidence/explanation`，先给 Schema 和完整字段骨架，再给证据；骨架中的占位符不是默认结论。提示词不是强制格式保证，仍须本地校验。

时序字段统一引用**观察到源码变化的事件**，其 `changed_paths_since_previous_event` 必须包含本案例 Java 源码路径；不能用编辑工具的调用开始事件代替结果快照。模型仍负责判断候选、实验及准入的含义，脚本不按顺序替它猜评审项，不擅自修正事件编号。

准备单个试次的完整评审输入，输出必须是 suite 外的新目录：

```bash
python3 -B evals/ci-self-healing/review_chrys.py prepare \
  --suite /absolute/path/to/eval-suite --trial trial-XXXXXXXXXXXX \
  --output /absolute/path/to/new-native-review \
  --profile-id ACTUAL_PROFILE_ID --endpoint-host gateway.example.invalid \
  --chrys-version ACTUAL_CHRYS_VERSION
```

这里声明已选 `huoshan-glm5.3` 的 profile ID、HTTPS 网关主机名与实际 Chrys 版本；这些值不是凭据，也不是运行成功证明。准备结果包含 `input/task.txt`、`input/packet.json`、`input/schema.json`、`binding.json` 和 `invocation.json`。后者仅保存计划的原生 CLI 参数，`models_called=false` 表示准备阶段未调用模型。

真正执行仍使用 `chrys run --agent QA --model huoshan-glm5.3 --task ... --workdir ... --json --quiet`，由另行获准的受控执行器提供文件/网络边界、时限、会话存储目录和进程回执。`--workdir` 不是沙箱，`--model` 是后备选择，QA 也可能被用户 profile 覆盖；必须核对实际会话。原生路径不继承 HTTP 适配器的无工具、禁止压缩、token 上限或网络保证。不要为审计打开可能记录凭据的原始 HTTP 日志。

执行器捕获 CLI 原始 stdout、真实进程状态（`status`、`returncode`、`duration_seconds`）及原生 `session.json` 后收集：

```bash
python3 -B evals/ci-self-healing/review_chrys.py collect \
  --directory /absolute/path/to/new-native-review \
  --stdout /absolute/path/to/execution/stdout.log \
  --process-file /absolute/path/to/execution/process.json \
  --session-file /absolute/path/to/native/session.json
```

收集到 `collected/`：原样保留 stdout 和公开 `result.txt`，回执只摘取必要元数据，不复制 session 的思维链、工具参数或任意内部属性。缺少会话、模型/网关不匹配、额外上下文、工具调用、压缩、输入漂移或输出不合规均明确记录；缺失记录用 `null`，不能当成“零调用”。输入、提示词、输出及会话文件用摘要关联，不截断证据，不自动补字段、去围栏或重试到通过。已有收集目录拒绝覆盖。

回执分开表达：

- `model_verdict`：模型原本声称的 `pass/fail/incomplete`。
- `output_status`：该公开结果是否满足字段、证据和时序合同。格式无效不等于自愈方向错误。
- `gaps`：执行/会话核对的缺口；`call_record_gaps`：未收集的服务端模型回执、完整请求/重试历史。客户端绑定不冒充服务端身份。
- `formal_evaluation_status`：此轻量收集入口保持 `incomplete`，`collect` 返回退出码 2，不写 trial 的 `review.json` 或升级 suite 分数。即使公开结果有效，也仅供人工复核；不是已实现正式原生评审自动验收。

历史失败输出保持原样，可离线回放检验校验器；新提示词需要另建评审目录。重新调用模型须明确执行，不能以“格式修复”为由修改旧判定或覆盖旧证据。新的 `test` / `native_runner.py` 另行采集原生 exchange 并接入严格验收；不会倒改本轻量收集器或自动把历史 incomplete 升级为通过。

## 一次试次做什么

1. 固定 Skill 内容摘要、案例 Git 历史、介入时文件和未跟踪的 Job 文件；两种 Skill 版本得到等价但独立的现场。
2. 在无网络沙箱副本中实际运行原 Maven 命令和 javac。必须确认 Feature 的参数不匹配，而非工具/依赖失败，才能启动模型。真实日志再写入现场，不使用伪造日志。
3. 默认通过本机 Chrys CLI 入口执行 GLM 5.2 自愈，每个试次限定自己的可写现场、运行配置和会话目录。Skill 的只读快照位于本次 `runtime/skills/ci-self-healing`，发现路径和任务提示一致；不全局安装、不暴露评分答案。
4. 启动模型前，在无网络沙箱调用 Chrys 原生 Skill 发现、`load_skill`、资源读取和内置搜索；失败记 `preflight_failed`，不启动模型、不用普通文件读取降级掩盖接入错误。正式会话还通过公开事件核对实际 Skill 名称和来源。观察公开消息、工具结果及文件边界状态，用 Session 元数据核对请求模型 ID。`run --json` 的最终回答不是完整轨迹。
5. 结束后独立检查文件和 Git HEAD。在另一无网络沙箱中对候选副本运行 Maven、直接 javac 和隐藏行为 Oracle；不在提交现场构建，不改变 Agent 已交付的工作空间。旧 Docker 后端执行相同逻辑但使用独立容器。
6. 使用评测器独立保存的 `contracts/v1` 报告合同快照检查格式，不自动采用 after Skill 中的校验器；修改合同需单独审核并升级评测版本。再将不含版本标签/执行模型身份的文件、公开事件、确定性结果发给 suite 绑定的评审模型（默认 GLM 5.3）。评审必须引用真实证据 ID；工具调用、缺失事件、格式错误、伪造引用和前后矛盾均不能算评审通过。
7. 按 before/after、案例、重复次数汇总 passed/failed/incomplete/not_run。确定性失败不能被评审覆盖；缺少语义评审不能算通过。结果只证明这一组模型/宿主/Skill/案例配置的样本表现。

清单交付合同由冻结 Skill 的 `assets/delivery-resources.json` 选择：`stop-artifact-v2` 保留历史逻辑仓库名规则，`stop-artifact-v3` 要求 `repo` 等于实际 Git root 的 basename。当前用例特意保留项目名 `routing-feature` 与 Git root 目录名 `sourcecode` 的差异，v3 只接受后者。两版都要求报告与清单完整交付、条目匹配最终源码差异，无净修改时 `changes: []`；JSON 文件版本仍为 `"0.1"`，共用独立冻结的 `contracts/file-change-v1` 格式校验器。v1 旧报告合同及历史运行记录保持原样，旧 v2 通过不能证明符合调度器的 basename 匹配要求。

## 自愈会话留存、超时与排障

新的运行合同为 `repair-runtime-v2`；只影响新 suite。旧记录保留原样，改过框架、镜像、profile 或任务路径后应重新 prepare，不在旧批次内续跑或覆盖证据。

- 每个 trial 的 `native-session/` 以权限 0700 创建，`CHRYS_SESSION_ROOT_DIR` 指向该宿主位置，超时不会删除已落盘记录。它不属于 `visible/` 源码现场，不参与 Patch／清理评分，也不暴露其他 session。旧 Docker 后端将同一目录单独挂到 `/artifacts/native-session`。
- 原生会话是私有诊断材料，可能含内部消息；不自动发送给评审、不打印或提交。`session-receipt.json` 仅记录文件摘要、有限身份元数据和完整性缺口；评分及续跑校验其绑定。正常退出、超时或中断都尽力生成该回执。只能保证已落盘文件不随容器清理消失，不能保证强杀前尚未写入的消息被保存；缺失、部分 JSON、符号链接、身份不符均不能当作完整记录。
- 模型实际收到的提示保存在只读 `adapter/task.txt`，由原任务加统一预算提示构成，摘要写入 `run.json`。按本次有效剩余预算告知 UTC 硬截止，预留最多 60 秒（通常为预算的 20%）收尾；不包含案例答案。预留是 Agent 可见约定，不保证其遵守，不会延期、自动重试或替 Agent 编造 `analysis-fix.md`。硬截止仍由宿主执行，超时仍为 incomplete。
- 公开轨迹增加 `transport.*`：仅记录 HTTPX 调用序号、单调时钟耗时、状态码、响应首字节／流结束和字节数、异常类型；不读取或记录 URL、Header、请求／响应正文。它用于区分工具耗时与传输等待，不证明服务端推理耗时；SDK 内部重试未必逐次可见，预缓冲响应也不会被伪装成完整流计时。
- `doctor` 检查 Chrys 实际选择的搜索二进制及原生接口；本机执行显式使用配置中的工具路径，不自动修改安装。旧 Docker 后端仍需自行准备兼容镜像，与默认本机路径无关。

## 判定与覆盖边界

- 不以精确 Patch 或增删行数为标准。保持契约案例验证任意输入仍返回基础结果；扩展案例验证三个参数均影响结果且保留兼容入口。证据不足案例允许独立获准实验，但不得遗留依赖未决选择的语义候选。
- 准入消息的内容允许自由表达；脚本不靠匹配“allow”单词证明有充分依据。评审模型判断事件是否构成有效准入及候选/实验归属，脚本再检查其引用存在性和时序。v1 的自动通过只接受可归属 Agent 的公开消息；Skill 允许的文件/工具日志式准入仍合法，但本版无法可靠区分自主发布记录与读取外部材料，记为 incomplete、待人工复核，不因此判执行违规。
- 观察器只覆盖公共事件和工具边界快照，不是不可绕过的系统调用审计；单个工具内部先改后恢复、任意外部后台进程及恶意篡改观察器不在确定性证明范围。评审须检查命令，不透明行为影响结论时记 incomplete。
- Chrys 观察器按 CLI 入口、异步事件发布签名和所需事件字段检查兼容性，不按版本号放行或拒绝。原生 Skill／搜索接口仍须实际预检；所选版本接口不兼容时报告环境缺口，不静默跳过观察或降级。宿主统一追加预算提示，观察器采集事件、检查注册并记录传输计时；不选择修复方向，但会增加采集耗时。真实试跑需要核查事件覆盖。缺失/未配对事件和模型身份缺口不算通过。
- 本版三个案例采用无交互 CI Job 场景，报告目录正常可写。文字降级报告、复杂多对象分类、交互暂停恢复等未做端到端判定；缺少文件报告时保留缺口，不猜测合规。
- 验证器与候选隔离，且源码、非 index Git 元数据、执行证据改变后拒绝复用旧结果；同一 suite 绑定后端、实际工具版本、专用缓存摘要、profile 内容摘要、请求模型和预算，旧 Docker 后端还绑定镜像和网络。Git index 的 stat 刷新被排除，未声称验证所有 index 语义。语义评审提示是防混淆指引，不是安全沙箱。Codex 用工具会使本次盲审失效；应使用受控评审环境，CLI 自身仍需要认证。
- 轨迹输出可能包含模型生成的任意文本。仅使用本仓合成材料，凭据只作环境注入；观察器替换已注入的测试密钥，但不能保证任意编码后的秘密被识别。不要把未经检查的运行目录提交或上传。

## 执行资源布局版本

执行审计按冻结包的 `assets/execution-resources.json` 选择资源合同，当前布局使用 `skill-execution-v6`，已声明 v3/v4/v5 的旧包继续使用原版本；更早的无声明旧包选择 v2。新入口引用拆分资源却缺少布局声明、声明未知或损坏时拒绝降级。已保存运行始终按自身记录的 v1/v2/v3/v4/v5/v6 合同复核，不改写历史试次。

v3 保持 v2 的准入/清理证据及恢复判据，只调整动作前的文档读取要求：候选源码写入前完整读取诊断、修复依据和准入指南；正常构建读取清理指南；已观察到基准时序违规后，重新采集前读取恢复指南。停在只读阶段不要求候选或清理分支。全文读取、原生工具来源、候选/范围绑定、恢复后重验及最终无漂移要求保持。脚本检测范围仍限于现有合成 Java 现场及直接 Maven/javac 构建，不宣称工具层强制隔离或所有语言的动作识别。

v4 继承上述要求，在观察到的 Java 源码变化和直接构建前增加固定 `enterprise-policy.md` 完整读取门槛，并在构建前检查 `candidate-verification.md`。策略未读/缺失不作为未配置，事后补读不抹去违规。只读停止不强制加载执行分支。策略适用性、候选与实验意图、候选修改前新指南及失败/协作指南的条件读取仍由语义评审核对；确定性审计是事后证据检查，不是写入前工具拦截，不解析策略自然语言来自动授权。

v5 继承 v4，在合成现场的文件/搜索工具、显式引用 trial 可见根或 cwd 的 shell、已识别构建和非帮助 Skill 脚本前，检查 `invocation-inputs.md` 的完整原生读取结果。仅发出读取请求、截断或事后补读均不能追认先前访问；不改变参数解析语义。元数据工具与脚本帮助不触发该门槛。未知工具、shell 动态构造路径、参数实际解释、修复专用合同适用性和停止是否充分仍由语义评审核对，不能把此门槛称为全工具拦截或全部指令遵从证明。无构建的停止不强制加载清理指南；已发生构建仍要求原有清理证据。多读指南只作为分支效率问题分析，不自动变成安全失败或从 token 数推断意图。

v6 保留 v5 的参数、策略、候选验证和恢复清理门槛，将独立 `repair-basis.md` 合并到 `repair-and-verification.md`。新布局在候选源码写入前检查合并指南的完整读取，只读停止不增加修复资源要求；仅返回准入末节、截断或事后补读仍不满足门槛。v3/v4/v5 继续要求其冻结包中的独立依据资源，v5 的参数读取门槛也保持不变。

评审摘录按同一合同选择章节：v1/v2/v3/v4/v5 保留各自原映射；v4 从候选验证、失败处理、协作和企业策略指南取得原文，v5 另外保留主入口的输入前置步骤与准入章节内的停止路由。缺失必需章节时报错，不默默丢弃语义判断或恢复条件；未完整读取的材料保持原样，未读材料不会凭空加入已读证据。v6 从合并指南中完整提取原有目标行为、修复位置、重要反证、语义歧义和完整候选缺口判据。五项评审结构保持不变。

`runtime-diagnostics.json` 的 timing 增加可选 `native_tool_operations`：按原生 operation ID 配对，分别统计开始、结束、结果类型、未配对项和白名单工具的计数；涵盖公共工具结果遗漏的 `invalid_arguments`。缺原生记录为 null，未知结果/重复/不配对留缺口；不导出参数、原始报错、任意工具名或私有推理，也不把 dispatch success 当语义正确。公共 `tool_results` 保留原口径。原生预检增加合成报告完整读回及数组/字符串参数正反例，不启动模型或自动修正模型参数。

## 验证框架自身

`tests/test_batch.py` 用明确标注的假后端自动验证整套重复运行、断点续跑、单例失败隔离、预算/共享故障停止、证据漂移与新增用例注册；它验证调度与防护，不证明模型遵守 Skill。原有测试覆盖 fixture、观察器、确定性评分和评审合同。离线测试通过、容器构建通过、真实模型行为通过是三种不同证据，不能互相替代。真实验收必须保留新批次的 summary 和原始回执，不复用旧单例结论代替整套结果。

`tests/test_parallel_batch.py` 使用线程屏障证明三个试次实际重叠执行，并通过本地子进程验证协调器取消、清理回执、会话总预算、未知预留不重放、同一试次阶段顺序和汇总时序。共享绑定竞争、线程预算隔离以及新冻结默认值也有离线测试；这些测试不启动模型、不读取真实 profile。

实现参考：[Codex 非交互命令](https://learn.chatgpt.com/docs/developer-commands#codex-exec)、[Agent 执行轨迹评测](https://developers.openai.com/api/docs/guides/agent-evals)。具体 CLI 能力以被记录的本机版本为准。

GLM 适配器使用兼容 Responses 的 [`text.format` 结构](https://developers.openai.com/api/docs/guides/structured-outputs)，兼容服务是否遵循该格式须实测，不由 OpenAI 文档替其保证。

## 本次资源清理

新原生执行使用 `owned-resource-cleanup-v1`，只回收当前宿主独占创建并登记的目录，不扫描或清理历史 `runs/`。源码仍由 Skill 的候选基准与 `workspace_check.py` 校验；宿主收尾不修改 `visible/`，也不替 Agent 补造源码清理成功。

- 独立构建把实际源码、Oracle、构建脚本/配置、缓存来源清单和命令保存到同级 `*-evidence/`，日志保留在原 `baseline-build/`、`candidate-build/`。只有进程组退出、保留清单落盘并读回后，才回收本次 `*-workspace/`。
- 当前 Java/Maven 用例显式选择 `maven_clean_plan`，在安全的独立副本中调用固定 `maven-clean-plugin:3.2.0:clean`，使用同一沙箱、离线缓存与配置，关闭 fast clean、显式启用错误返回并限制时长。缓存缺插件时记录 skipped；执行失败保留其日志。随后仍由宿主清理登记目录并复核不存在，不将 clean 退出码当作完整性证明。旧缓存不自动联网补全；新环境显式 `--warm-cache` 才准备插件及依赖。
- 修复阶段回收本次 `runtime/tmp`、`runtime/java-home`、`runtime/m2`，保留共享原始缓存、实际配置、Skill、任务、会话/轨迹及诊断。评审阶段只回收本次专用 `tmp`，保留完整评审输入和会话证据。既存目录不接管为可删除内容。
- `resource-cleanup/<stage>/` 保存创建意图、目录身份、保留清单与最终回执。`cleaned` 表示该声明范围回收且保留证据复核通过；`incomplete` 保留失败/残留；未创建资源为 `not_applicable`。回执绑定进入运行、验证、评审及汇总，清理未完成不允许整次 passed，原始编译、超时或评审结果仍保留。
- 部分初始化和异常退出经 context 收尾；缺少进程退出证据时不盲删。强杀宿主可能只留下创建记录，需后续人工核对；当前没有按历史目录猜测归属的自动恢复入口。文件回收预算有界，不能处理仍在写入或身份已被替换的目录。

离线验证：普通框架测试包含真实临时文件系统、故障注入和构建接入测试；真实工具链集成使用隔离缓存及 Maven 离线模式，不调用模型。历史版本没有该收据时保留原判据，不追溯升级其清理结论。

### 通用原生清理接口与语言边界

`native_clean.NativeCleanPlan` 由宿主按实际构建上下文构造：记录构建系统、完整 argv、cwd、经清理的原构建环境、离线前置文件及有界超时。`clean_build(..., plan=...)` 只在本次登记的独占工作空间中运行该计划，命令和工作目录不被改写；没有已确认计划则跳过原生命令，仍可执行通用目录回收。入口不根据扩展名或存在 pom.xml 推断工具，不默认回退 Maven，也不把 shell 命令字符串直接执行。

沙箱可通过 `build_tools` 复用已确认的非 Java 工具安装路径，无需 Maven/JDK 配置，写入仍仅限本次独占目录，网络仍关闭。Make、CMake、Cargo、Gradle、Go、.NET、包管理器脚本或其他项目任务均可提供同一结构的计划；这表示执行接口不限定语言，不代表这些工具的版本、脚本语义与完整自愈场景已全部验收。当前三个模型修复用例仍是 Java/Maven，不将它们的结果扩展到其他语言。

新增测试区分通用参数传递和真实工具运行：`test_native_clean_plan.py` 的无模型合成测试核对不同构建系统的原命令/环境、无 clean 情况及范围拒绝；`CI_CLEANUP_TEST_NON_JAVA=1` 启用 macOS 沙箱内真实 C/Make 构建清理、Python 字节码/项目清理任务和越界删除阻止验证。真实 Maven 集成仍使用显式离线缓存变量。未安装的构建工具不下载或冒充已验证。
