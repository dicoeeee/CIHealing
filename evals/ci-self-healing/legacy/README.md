---
ai_generated: true
reviewed: false
---

# 历史清理合同兼容资源

`cleanup-v7/skill/` 是 2026-11-05 取消强制编译产物清理前的完整本地 Skill 快照，`manifest.json` 记录原字节摘要。仅用于旧合同的离线兼容测试与历史审查，不属于新版 Skill 运行包，不供新修复调用发现或加载。

新版使用 `skill-execution-v8`：普通编译产物可以保留，不再要求全量 capture、clean、check 或最终 match。源码保护、候选撤销、必要验证和企业交付仍适用。v1–v7 的评测规则保持原含义，历史 trial 使用各自冻结包，不回填本目录、不重写原评分或运行结果。

评测宿主的进程停止、独占验证副本回收与证据保留继续有效。本目录进入新评测引擎摘要及冻结文件清单；原实验冻结目录和原始结果不变。`tests/test_legacy_workspace_check.py` 显式加载归档单测，避免从新版运行包引用已退役脚本。
