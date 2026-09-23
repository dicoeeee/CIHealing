# GitHub Actions 证据访问

## 身份与范围

Provider ID 为 `github_actions`，适用于可信调用明确指定的 GitHub Actions 运行。主机和 `OWNER/REPO` 来自该目标的可信信息；源码托管在 GitHub 不证明 CI 使用 GitHub Actions。GitHub Enterprise Server 的端点支持还须匹配实际服务版本。

本文件提供只读接入说明，不是已部署的适配器。访问顺序、缺口与边界统一按 [Provider 访问](provider-access.md)；逻辑能力使用 `identify_run`、`inspect_run` 和 `fetch_failure_evidence`。

## Local Evidence

优先读取调用方已提供的 run/Job/Attempt metadata 和日志。本 Provider 不假设固定日志文件名；宿主已选择[失败现场目录场景](../ci-job-failure-directory.md)时复用该布局。核对主机、仓库、运行、重试与实际源码状态，已有证据足够时无需调用下方工具。

## MCP

只使用宿主明确归属于目标 GitHub 服务的 MCP。按实际工具说明确认它能解析指定运行/重试、列出该重试的 Jobs、读取 Job metadata 和完整日志，并能传递目标仓库及身份。仅有仓库或 PR 工具不证明具备 Actions 日志能力；本文件不假设存在某个固定函数名。能力缺失时使用下方 CLI 补齐，不自动安装 MCP。

## CLI

已声明的 CLI 是 `gh`。先运行 `command -v gh`，按需检查 `gh api --help`。以下是通过 GitHub CLI 调用 REST 的只读示例；`HOST`、`OWNER`、`REPO`、`RUN_ID`、`ATTEMPT` 和 `JOB_ID` 均须用已核对的目标值替换并正确引用。`HOST` 是 GitHub 主机（如 `github.com`），不得借用当前目录或默认账户推定目标仓库。

```sh
gh api --hostname HOST --method GET 'repos/OWNER/REPO/actions/runs/RUN_ID/attempts/ATTEMPT'
gh api --hostname HOST --method GET --paginate 'repos/OWNER/REPO/actions/runs/RUN_ID/attempts/ATTEMPT/jobs'
gh api --hostname HOST --method GET 'repos/OWNER/REPO/actions/jobs/JOB_ID'
gh api --hostname HOST --method GET 'repos/OWNER/REPO/actions/jobs/JOB_ID/logs'
```

按当前缺口选用，不要求重复执行已有 metadata 查询。请求显式使用 GET；`--paginate` 用于取得目标重试的 Job 列表。使用已有认证且不扩大权限，不加 `--cache` 将旧响应当作当前观察。[GitHub CLI API 文档](https://cli.github.com/manual/gh_api)

## 失败绑定与完整性

1. 核对指定重试响应的运行 ID、`run_attempt`、仓库和 `head_sha`；工作流 ID、展示编号不替代 run ID。未指定重试时先解析目标并固定，不能用最新重试静默替换请求中的旧失败。[运行重试 API](https://docs.github.com/en/rest/actions/workflow-runs#get-a-workflow-run-attempt)
2. 从该重试的完整 Job 列表核对目标 `JOB_ID`、矩阵项、状态和步骤，再取 Job 日志。即使 Job 名称相同也按具体 ID 绑定；没有明确目标且有多个失败 Job 时分别保留，不能任选一个代表全部。[重试 Job 列表 API](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt)
3. Job 日志接口返回短时下载重定向，成功取得日志正文后才记为已获取；仅拿到 URL 不等于取得日志。截断、过期、空响应或缺失步骤如实记录，不用另一重试补齐。日志未给出失败命令或退出码时保留未知。[Job 日志 API](https://docs.github.com/en/rest/actions/workflow-jobs#download-job-logs-for-a-workflow-run)

`head_sha` 是运行观察，仍须与失败现场的实际源码状态核对；本地已有 Job 修改不能被该 SHA 抹去。CLI 使用不改变验证模式，日志读取不触发 rerun、workflow dispatch 或候选发布。

## 访问不可用

`gh` 未安装、现有凭据不可访问目标、Actions 能力未暴露或日志不可用时，按[不可用与交付](provider-access.md#不可用与交付)记录缺口。404 本身不足以区分无权限与目标不存在；不自动登录或改认证，不通过重跑来生成新日志冒充原失败。

本文件的命令依据官方文档；实际宿主的 MCP、CLI 权限、重定向行为和目标日志仍须以实际执行证据核对，示例不证明当前接入可用。本文件不声明远端发布或完整适配器已通过验收。
