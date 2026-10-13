---
ai_generated: true
reviewed: false
---

# 本地隔离评测环境

本目录只供评测维护者使用，不属于被测 Skill。环境准备、连通性冒烟、真实自愈试次和语义评审是不同步骤；前两项通过不能声称后两项完成。

## 默认：直接使用本机 Chrys

新测试使用 `backend: native`，不构建镜像、不安装 wheel、不启动 Docker/Colima。按[本机环境说明](native.md)准备独立缓存和沙箱，再通过 `eval.py test` 一次运行整套回归。自愈、编译验证和评审都复用本机工具；不修改全局 Chrys 或 JDK 配置。

以下内容仅用于显式选用旧 `backend: docker` 的兼容路径和历史复核，不是本机后端的前置条件。新旧后端不自动互相回退。

## 旧 Docker 后端：资源与隔离

- 专用 Colima profile/context：`ci-self-healing-eval` / `colima-ci-self-healing-eval`，不切换全局 Docker context，不操作其他 profile。
- 镜像：默认 Python 3.14、显式选择的 Chrys 安装包、JDK 21、Maven、Git、rg；Chrys 不设固定版本，所选包的 Python 要求必须与基础镜像相容。构建后将真实 image ID 固定在 runtime 中。基础镜像固定 digest，重建时 apt/Python 传递依赖仍可能更新，不能把 Dockerfile 当作字节级重现证明。
- 模型容器仅连接 `ci-eval-internal`。网关连接该 internal 网络和独立 egress 网络，不发布宿主端口；只转发已选模型的 `/v1/responses`，拒绝重定向、其他模型、远端存储引用、托管工具和媒体获取。
- 真实上游 Key 只经 stdin 写入网关的一次性 tmpfs FIFO，再驻留网关内存；不写入源码、runtime、镜像、Docker 环境变量或命令参数。被测 Agent 的 `CHRYS_EVAL_API_KEY` 只是固定的本地网关标识，不是上游凭据。此标识不是多租户认证机制。
- 网关最多受理 256 次请求、并发 2；不是 token/费用上限。上游请求最长读等待 180 秒。无需评测时停止网关，避免保留凭据驻内存的进程。重启需要重新注入凭据。
- 仅用于同一可信宿主上的合成案例；不是恶意多租户安全产品。Docker 管理员仍能访问容器，stdin/in-process 观察器也不是防管理员的秘密边界。

## 准备

准备需要使用的 Chrys 完整 wheel（非 editable wheel，保留真实文件名），放进新构建上下文的 `wheels/` 子目录；该目录只允许一个 Chrys wheel，Dockerfile 不固定或自动获取某个 Chrys 版本。不要将私人配置放入构建上下文。Colima 只共享本仓 `evals/ci-self-healing/runs`，suite 与临时挂载目录均须位于该目录下。

```bash
colima start ci-self-healing-eval \
  --cpus 4 --memory 4 --disk 30 --runtime docker \
  --vm-type vz --mount-type virtiofs \
  --activate=false --template=false --ssh-config=false \
  --mount /absolute/repo/evals/ci-self-healing/runs:w

mkdir -p evals/ci-self-healing/runs/environment/build-context-new/wheels
cp evals/ci-self-healing/environment/Dockerfile \
  evals/ci-self-healing/environment/debian.sources \
  evals/ci-self-healing/environment/cache-pom.xml \
  evals/ci-self-healing/environment/CacheSeed.java \
  evals/ci-self-healing/environment/gateway.py \
  evals/ci-self-healing/runs/environment/build-context-new/
cp /absolute/path/to/selected-chrys-wheel.whl \
  evals/ci-self-healing/runs/environment/build-context-new/wheels/
docker --context colima-ci-self-healing-eval build \
  --build-arg EVAL_UID="$(id -u)" --tag ci-self-healing-eval:local \
  evals/ci-self-healing/runs/environment/build-context-new
```

如果本地到 PyPI 反复断流，可显式增加 `--build-arg EVAL_PYPI_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple` 使用清华同步源；不要关闭 TLS 验证。此选择只影响镜像构建，测试容器仍不允许在线安装依赖。

使用现有 Chrys Python 执行管理脚本（需要 PyYAML 和 Chrys）；以下路径均替换为本机实际值。`configure` 不覆盖目录，`gateway` 不覆盖现有容器，失败后先检查再决定重建。

```bash
EVAL_PYTHON=/absolute/path/to/chrys/bin/python
EVAL_RUNTIME=/absolute/repo/evals/ci-self-healing/runs/environment/runtime/runtime.json

"$EVAL_PYTHON" -B evals/ci-self-healing/environment/manage.py configure \
  --model-profile /absolute/path/to/selected-glm-profile.yaml \
  --image ci-self-healing-eval:local \
  --output /absolute/repo/evals/ci-self-healing/runs/environment/runtime
"$EVAL_PYTHON" -B evals/ci-self-healing/environment/manage.py gateway \
  --model-profile /absolute/path/to/selected-glm-profile.yaml --runtime "$EVAL_RUNTIME" \
  --pin-upstream-dns
"$EVAL_PYTHON" -B evals/ci-self-healing/environment/manage.py exec \
  --runtime "$EVAL_RUNTIME" -- doctor --runtime "$EVAL_RUNTIME"
```

`EvalCode` 复用已安装版本的 Code 指令，关闭子 Agent、交互工具、自动加载其他 Skill 和记忆；仅开放文件、搜索、shell、sleep、todo 及本次 Skill。指令摘要写入 `setup.json`，不是为期望答案增加的专门提示。Chrys profile 的环境变量语法为 `{{CHRYS_EVAL_API_KEY}}`。

Skill 的规范目录名必须与 frontmatter `name: ci-self-healing` 相同，因此 `configure` 固定 `skills.paths: [/skills/ci-self-healing]`，运行时将本次冻结副本只读挂到该路径，任务也引用同一路径。不会修改 Skill 的 name、扫描宿主全部 skills 或安装到全局。旧 profile 的 `/skill` 不会被静默兼容；请用新的输出目录（例如 `runtime-v2`）重新 configure，并更新 batch 配置。旧 runtime、suite 和运行证据保持不变。

Dockerfile 在安装 wheel 后，若存在 Chrys vendor `rg`，用 Debian 同平台 `/usr/bin/rg` 替换；不存在时不要求该布局，仍测试 Chrys 实际选中的路径。升级时需要显式重建镜像、固定新 image ID；旧镜像不会因 Python 框架变更自动更新。`doctor` 检查实际版本、headless CLI、事件接口和内置搜索，试次在启动模型前另行检查实际 Skill 副本与 profile 的原生注册／资源读取，任一必需能力缺失均不调用模型。

`--pin-upstream-dns` 用于虚拟机缺少外部 DNS 的场景：只将宿主机解析出的指定上游 IPv4 地址写入本次网关的 hosts 配置，URL、SNI 与证书校验仍使用原域名，不固定任意额外目标。上游 DNS 变更或 VPN 切换后需重新创建网关以刷新；不会修复或更改虚拟机全局 DNS。虚拟机 DNS 正常时可省略此选项。

## 验收与运行

可以先做完全离线的原生 Skill 兼容检查（使用已有完整依赖，不安装 wheel、不启动 Docker 或模型、不读取用户 profile）：

```bash
"$EVAL_PYTHON" -B evals/ci-self-healing/environment/native_compat.py
# 可选：只解包检查某份已审核的 wheel，不安装；依赖复用当前 Python 环境。
"$EVAL_PYTHON" -B evals/ci-self-healing/environment/native_compat.py \
  --wheel /absolute/path/to/selected-chrys-wheel.whl
"$EVAL_PYTHON" -B -m unittest discover -s evals/ci-self-healing/tests -v
"$EVAL_PYTHON" -B -m unittest discover -s evals/ci-self-healing/environment -p 'test_*.py' -v
```

`native_compat.py` 默认使用当前 Python 环境中的 Chrys；给 `--wheel` 才在临时目录解包指定包，不检查版本号白名单。它记录实际版本，检查事件接口和原生 Skill 工具，验证正确目录名可用、旧 `/skill` 命名必须失败；日志出现该负例的 load warning 属于预期。它不代替 Linux 容器的 `doctor`、真实会话事件或端到端试次。使用的 wheel 必须是已审核的本地构建产物。

`smoke.py` 默认在无网络容器中验证真实原始编译失败及已知合成变体的编译/行为检查，不调用模型。`--chrys-live` 单独执行一个“读 probe.txt 并回答 READY”的小任务，核验模型连接与公共工具事件；**不是自愈案例**。输出路径须不存在。

```bash
"$EVAL_PYTHON" -B evals/ci-self-healing/environment/smoke.py \
  --runtime "$EVAL_RUNTIME" \
  --output evals/ci-self-healing/runs/environment/build-smoke
"$EVAL_PYTHON" -B evals/ci-self-healing/environment/smoke.py \
  --runtime "$EVAL_RUNTIME" --network \
  --output evals/ci-self-healing/runs/environment/network-smoke
"$EVAL_PYTHON" -B evals/ci-self-healing/environment/smoke.py \
  --runtime "$EVAL_RUNTIME" --chrys-live \
  --output evals/ci-self-healing/runs/environment/chrys-smoke
```

`network_probe.py` 需在仅连接 internal 网络的临时容器中运行；它验证网关拒绝规则，并尝试公共地址及宿主代理端口。单次探针不构成全部网络路径的形式化证明，还需核对容器挂载、网络和无 published ports。

后续调用 `eval.py` 统一经 `manage.py exec --runtime ... -- <子命令>` 包装：只为子进程设置专用 `DOCKER_CONTEXT`、网关标识和已共享的 `TMPDIR`。否则 macOS 默认临时目录可能不在虚拟机共享范围内，导致行为验证器挂载失败。`run`/`review` 的 `--execute-live` 仍须显式提供，不会因环境就绪而自动运行完整矩阵。

日常回归使用 `manage.py exec --runtime ... -- test --config ... --execute-live`，一次完成整套自愈、确定性检查、原生 Chrys / GLM 5.3 评审和汇总。`test` 的宿主评审要求 macOS、具备所需 CLI／轨迹能力的 Chrys、已选 GLM 5.3 profile 及现有轨迹 HMAC key；不要求固定 Chrys 版本，也不更改上述 GLM 5.2 容器执行网关。配置、预算和续跑见上级评测说明。

以下保留旧分阶段 `eval.py review --review-profile ...` 的 **HTTP** 校准方式，只用于对应适配器测试，不是 `test` 原生评审路径的验收。它通过可信客户端注入真实认证，模型不持有凭据。

可独立进行一次真实评审通道校准（合成的已知违规轨迹，不是实际自愈）：

```bash
"$EVAL_PYTHON" -B evals/ci-self-healing/environment/review_smoke.py \
  --profile /absolute/path/to/huoshan-glm5.3-profile.yaml \
  --output evals/ci-self-healing/runs/environment/glm53-review-smoke \
  --max-packet-bytes 524288 --max-output-tokens 16384 --timeout 600 \
  --execute-live
```

输入字节、输出 token 与墙钟预算与正式 GLM 评审使用相同默认值和边界；输出目录须不存在。完整 packet、stdin 封装及实际 HTTP 请求分别校验，不裁剪证据。校准必须同时满足：传输完整、Schema/证据/时序校验通过，且模型将已知违规轨迹判为 fail；不能仅以 HTTP 200 或进程退出成功验收。失败时查看 `result.json` 的 error/diagnostics，以及 `execution/stdout.log`、实时更新的 `stderr.log`。没有自动重试、加预算或更换模型；一次合成负例通过仍不等于完整自愈矩阵通过。

旧 Sol suite 仍需要单独验证 GPT-6 Sol CLI 访问。`codex login status` 成功不代表账号能使用该模型；不能静默改写旧 suite，也不能把应用内 Sol 子 Agent 的可用性视作 CLI 可用性。

## 停止与恢复

```bash
docker --context colima-ci-self-healing-eval stop ci-eval-gateway
colima stop ci-self-healing-eval
```

恢复 VM 后，网关需要重新启动并注入凭据。先核查旧容器属于本评测（label `ci-eval-owned=true`）、已停止且不再使用，再显式移除该网关容器并执行 `manage.py gateway`。不要删除其他实例、网络或评测证据。`runs/`、wheel、日志和本地 runtime 均不得提交；每次 suite 的记录继续固定实际 image/network/profile 摘要。

依赖来源：[Docker 官方 Python 镜像的公共仓库](https://gallery.ecr.aws/docker/library/python)、[Debian 官方镜像列表](https://www.debian.org/mirror/list-full)（包含本构建使用的清华镜像）。下载保留 TLS、Debian 签名及包校验，不使用跳过校验的安装参数。
