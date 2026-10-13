"""Shared repair-runtime layout and private diagnostic receipts (not grading answers)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import EvalError, digest, read_json, read_regular, snapshot

SKILL_NAME = "ci-self-healing"
SKILL_MOUNT = "/skills/" + SKILL_NAME
SESSION_MOUNT = "/artifacts/native-session"
ADAPTER_FILES = ("common.py", "repair_support.py", "chrys_capabilities.py", "chrys_preflight.py", "repair_telemetry.py", "chrys_observer.py")
RUNTIME_CONTRACT = "repair-runtime-v2"
LEGACY_EXECUTION_CONTRACT = "skill-execution-v1"
RECOVERY_EXECUTION_CONTRACT = "skill-execution-v2"
EXECUTION_CONTRACT = "skill-execution-v3"
EXECUTION_CONTRACTS = {LEGACY_EXECUTION_CONTRACT, RECOVERY_EXECUTION_CONTRACT, EXECUTION_CONTRACT}
RECOVERY_EXECUTION_CONTRACTS = {RECOVERY_EXECUTION_CONTRACT, EXECUTION_CONTRACT}
EXECUTION_RESOURCE_DECLARATION = {"schema": "ci-skill-execution-resources/v1", "contract": EXECUTION_CONTRACT}
ADMISSION_CHANNEL_CONTRACT = "agent-message-v1"
DELIVERY_CONTRACT = "stop-artifact-v1"


def execution_contract(skill):
    """Select the audit layout from the frozen package, never the live checkout.

    Old packages keep v2, including recovery semantics. A new-layout entrypoint
    without its declaration is incomplete, not an invitation to downgrade.
    Explicit historical run receipts continue to select their original version.
    """
    skill = Path(skill)
    declaration = skill / "assets/execution-resources.json"
    if not declaration.exists() and not declaration.is_symlink():
        if "references/repair-basis.md" in read_regular(skill / "SKILL.md").decode():
            raise EvalError("split Skill resource declaration missing")
        return RECOVERY_EXECUTION_CONTRACT
    if read_json(declaration) != EXECUTION_RESOURCE_DECLARATION:
        raise EvalError("unsupported Skill resource declaration")
    return EXECUTION_CONTRACT


def admission_channel_notice():
    """Host publication channel, not a decision, schema, or case-specific hint."""
    return ("\n宿主准入记录通道（" + ADMISSION_CHANNEL_CONTRACT + "）："
            "repair_admission（包括 experiment_only）必须在依赖该决定的动作前，"
            "直接作为面向宿主的普通 assistant 文本消息发出；宿主将其记录为 InvocationMessage。"
            "不要用 shell 的 echo/cat/printf 回显、写文件、工具调用参数或工具返回代替这条消息。"
            "最终报告不替代事前准入，动作后补发不能追认已有修改。"
            "这只是发布通道约定，不规定放行结论、不新增表单；"
            "准入内容、证据和停止条件仍按 Skill 判断。\n")


def source_state(files):
    """Fixture source inventory, including the root and empty/ignored directories."""
    prefix = "scene/sourcecode"
    return {"." if p == prefix else p[len(prefix) + 1:]: v for p, v in files.items()
            if p == prefix or p.startswith(prefix + "/")}


def skill_tool_notice():
    """Native-tool protocol only; no case answers or repair-policy overrides."""
    return ("\n宿主提供原生 Skill 工具：先用 load_skill 加载 ci-self-healing；"
            "包内资源用 read_skill_resource 按需读取，脚本用 run_skill_script 执行，"
            "不通过文件或 shell 工具直接访问包内资源/脚本。"
            "脚本名保留 scripts/ 前缀，命令行参数按原顺序传入 arguments 数组，cwd 使用实际绝对目录。"
            '\n原生调用示例（查看帮助，不是修复决定）：run_skill_script '
            '{"skill_name":"ci-self-healing","script_name":"scripts/workspace_check.py",'
            '"arguments":["capture","--help"]}。'
            "其他脚本同样替换 script_name 和 arguments；不要在参数中加入 python、shell 或脚本绝对路径。"
            "脚本结果需保留完整 JSON 和退出码；资源或结果被截断时补齐，不以摘要代替。"
            "目标仓库的普通文件读写、搜索、构建和清理仍用相应普通工具。\n")


def budget_notice(seconds, now=None):
    """Same neutral budget policy for every case; no case-specific stop hints."""
    now = now or datetime.now(timezone.utc)
    reserve = min(60, max(1, seconds * 0.2))
    deadline = now + timedelta(seconds=seconds)
    wrap_up = deadline - timedelta(seconds=reserve)
    return (f"\n宿主执行预算：最多 {seconds:g} 秒；硬截止 UTC {deadline.isoformat()}。\n"
            f"请在 UTC {wrap_up.isoformat()} 前结束新增探索，预留约 {reserve:g} 秒整理现场和交付报告。"
              "预算不改变 Skill 的准入、授权与验证判据；如无法完成，按实际证据报告未完成项，不得伪造成功。\n"
            + skill_tool_notice() + admission_channel_notice())


def session_receipt(root):
    """Hash private artifacts; export only allowlisted meta, never messages/reasoning."""
    root = Path(root)
    try:
        files = snapshot(root, skip_git=False)
        if any(row["kind"] == "symlink" for row in files.values()):
            raise EvalError("session artifacts contain a symlink")
    except (OSError, EvalError):
        return {"status": "incomplete", "gaps": ["session_inventory_unavailable"], "files": {}, "sessions": []}
    sessions, gaps = [], []
    for name, row in files.items():
        if row["kind"] != "file" or Path(name).name != "session.json":
            continue
        try:
            meta = read_json(root / name).get("meta", {})
            if not isinstance(meta, dict) or not meta.get("session_id"):
                raise ValueError("missing session identity")
            sessions.append({"path": name, "sha256": row["sha256"],
                             "meta": {k: meta.get(k) for k in ("session_id", "model_id", "model_profile_id", "app_version")}})
        except (OSError, ValueError, AttributeError, EvalError):
            gaps.append("invalid_session_checkpoint")
    if not sessions:
        gaps.append("session_checkpoint_missing")
    return {"status": "retained" if not gaps else "incomplete", "inventory_status": "captured", "gaps": sorted(set(gaps)),
            "files": files, "sessions": sessions,
            "coverage": "last persisted native checkpoints; not proof of a complete run or immutable observer"}


def validate_repair_artifacts(trial, run):
    """New receipts bind private diagnostics; legacy trials remain unchanged."""
    from resource_cleanup import cleanup_complete
    cleanup_complete(run)  # Integrity first; incomplete is a separate result.
    if run.get("runtime_contract") != RUNTIME_CONTRACT:
        return
    trial = Path(trial)
    if run.get("diagnostics_sha256") and digest(read_regular(trial / "runtime-diagnostics.json")) != run["diagnostics_sha256"]:
        raise EvalError("repair runtime diagnostics changed")
    if digest(read_regular(trial / "session-receipt.json")) != run.get("session_receipt_sha256"):
        raise EvalError("repair session receipt changed")
    receipt = read_json(trial / "session-receipt.json")
    if receipt.get("inventory_status") == "captured" and snapshot(trial / "native-session", skip_git=False) != receipt["files"]:
        raise EvalError("repair session artifacts changed")
    if digest(read_regular(trial / "adapter/task.txt")) != run.get("task_sha256"):
        raise EvalError("repair runtime task changed")
    if run.get("delivery_contract") == DELIVERY_CONTRACT:
        from delivery_evidence import assess_delivery
        if run.get("delivery_assessment") != assess_delivery(trial, run):
            raise EvalError("delivery evidence changed")
