# CI 自愈诊断与修复报告

| 字段 | 内容 |
| --- | --- |
| invocation_id | $invocation |
| 请求类型 | 修复 |
| 执行模式 | local_verify |
| 处理结果 | $outcome |
| 绑定 Profile | 宿主指定 authentication-upload v1 |
| work_dir | $scene |
| 构建工作空间 | $scene/sourcecode；来源为宿主本次独占任务分配，本场景未执行修改或验证 |
| output_dir | $output |

| 仓库 | 源码绝对路径 | baseline_sha | 候选 fingerprint |
| --- | --- | --- | --- |
| application | $scene/sourcecode | $baseline | 无候选 |

## 1. 处理结论

扫描上传时机器账户认证被拒。$lookup

## 2. 失败与根因

Job 10101 的 error_logs.txt 记录了 HTTP 401、invalid_client 和 machine-credential。具体认证失败原因仍需服务端审计。

| 判断 | 分类路径（标准中的各级编号和名称） | 依据与未确定项 |
| --- | --- | --- |
| 初始分类 | Q1 质量检查 / Q1.1 外部服务访问失败 / Q1.1.1 机器账户认证被拒 | 扫描上传时机器账户收到 401。 |
| 最终分类 | Q1 质量检查 / Q1.1 外部服务访问失败 / Q1.1.1 机器账户认证被拒 | invalid_client 支持初始判断。 |

## 3. 目标行为与修复依据

上传须使用有效的机器账户，凭据由管理员维护。

## 4. 实际修改

本次未修改源码、配置或凭据。

## 5. 验证结果

本次没有候选，未执行构建或重试上传。

## 6. 剩余问题与交接

凭据管理员按 Job 10101 查询服务端审计，确认拒绝原因后决定恢复动作。
