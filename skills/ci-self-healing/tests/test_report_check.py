"""Report delivery scenarios and read-only CLI behavior; synthetic evidence only."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "report_check.py"
SPEC = importlib.util.spec_from_file_location("report_check", SCRIPT)
report = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = report
SPEC.loader.exec_module(report)

BASE = """# CI 自愈诊断与修复报告

**记录信息**

| 字段 | 内容 |
| --- | --- |
| invocation_id | example-1 |
| 请求类型 | 修复 |
| 执行模式 | local_verify |
| 绑定 Profile | 未绑定：Skill 未固定，本次调用未指定 |
| work_dir | /scene |
| output_dir | /output |

**仓库与候选**

| 仓库 | 工作空间绝对路径 | baseline_sha | 候选 fingerprint |
| --- | --- | --- | --- |
| example | /scene/source | abc123 | digest-example |

## 1. 处理结论

- 当前结论：local_verified
- 结论说明：本地编译和适用检查通过；未执行外层流水线。
- 调用情况：本次调用结束。

## 2. 失败与根因

### 失败对象：example/build

- 原始失败：编译退出 1，缺少符号。
- CI 证据：本地已绑定的 /scene/error.log:10，完整性未知。
- 根因判断：缺少导入；参见 src/Example.java:2 和 MR-1 的契约说明。
- 分类要求：无：未配置 Profile 且本次无分类要求。

## 3. 目标行为与修复依据

- 目标行为：恢复契约要求的类型引用。
- 依据来源：MR-1 描述及类型定义。
- 修复方向与约束：补充导入，保持契约。
- 相反方向与反证：未发现有据的相反方向，已核对 MR-1。
- 修复准入：实际编辑前宿主记录 admission-1。
- 最终复核：当前候选仍符合依据；参见 review-1。
- 企业策略：未配置。

## 4. 实际修改

- 修改情况：本次补充一处导入。

| 文件/位置 | 修改归属 | 实际修改及作用 | 交付状态与依据 |
| --- | --- | --- | --- |
| example/src/Example.java | 本次修改 | 补充导入 | 保留；见 diff-1 |

- 清理处置：删除本次构建生成的 target；见 cleanup-1。

## 5. 验证结果

- 检查情况：编译与清理校验已完成。

| 检查或命令 | 执行位置/目录或运行身份 | 结果 | 覆盖范围与未覆盖内容 | 证据 |
| --- | --- | --- | --- | --- |
| mvn compile | /scene/source | passed | 当前候选本地编译，不含外层 CI；见 receipt-1 的环境和时间 | build.log:20 |
| workspace_check check | /scene/source | match | 当前根目录和 candidate baseline；时间见 cleanup-1 | cleanup-1 |

- 工作空间校验：根 /scene/source，清单及原摘要见 capture-1；当前候选绑定、match 和时间见 cleanup-1。

## 6. 剩余问题与交接

- 剩余问题：外层 CI 尚未验证。
- 继续条件：宿主执行原定后续检查。
- 交接情况：无交接：当前无已授权通道。
- 产物与后续责任：源码留在工作空间，后续脚本负责提取 Patch。
"""

CLASSIFICATION = """- 分类要求：有：可信调用指定 example-profile。

- Profile 标识：example-profile
- Profile 版本：v1
- Profile 入口：profiles/example.md
- Profile 绑定来源：可信调用参数。

| 判断 | 分类路径（标准中的各级编号和名称） | 依据与未确定项 |
| --- | --- | --- |
| 初始分类 | C 编译；小类未确定 | error.log:10；当时仅支持父类 |
| 最终分类 | C 编译 / C1 符号解析 | src/Example.java:2；符合 C1 定义 |

- 分类变化及原因：补取源码后确定小类。
- 最终分类交付：已完成。
"""

REMOTE = """**远端运行**

| 仓库/候选 | 专用 ref | 候选 SHA | 触发方式 | Provider 运行身份 | 最近观察时间 |
| --- | --- | --- | --- | --- | --- |
| example/digest-example | candidate/example-1 | def456 | push | example-provider/run-2/job-1 | 2026-09-28T08:00:00Z |

"""


def classified(text=BASE):
    text = text.replace("| 绑定 Profile | 未绑定：Skill 未固定，本次调用未指定 |",
                        "| 绑定 Profile | example-profile；版本 v1；来源：可信调用参数 |")
    return text.replace("- 分类要求：无：未配置 Profile 且本次无分类要求。\n", CLASSIFICATION)


def split_checks(text=BASE):
    header = "| 检查或命令 | 执行位置/目录或运行身份 | 结果 | 覆盖范围与未覆盖内容 | 证据 |\n| --- | --- | --- | --- | --- |"
    return text.replace("| workspace_check check |", "\n### 工作空间校验：example/digest-example\n\n" + header + "\n| workspace_check check |")


def no_checks(text=BASE):
    before, section = text.split("## 5. 验证结果\n", 1)
    _, after = section.split("## 6. 剩余问题与交接\n", 1)
    return before + """## 5. 验证结果

- 检查情况：未执行：尚未形成验证计划，未运行检查。
- 工作空间校验：不适用：未进行工作空间写入。

## 6. 剩余问题与交接
""" + after


def no_changes(text):
    before, section = text.split("## 4. 实际修改\n", 1)
    _, after = section.split("## 5. 验证结果\n", 1)
    return before + """## 4. 实际修改

- 修改情况：未修改：在修复准入前结束。
- 清理处置：不适用：未进行工作空间写入。

## 5. 验证结果
""" + after


class ReportCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = report.TEMPLATE.read_text(encoding="utf-8")

    def errors(self, text, classification="none", checks="present"):
        return report.validate(text, self.template, classification, checks)

    def codes(self, text, **kwargs):
        return {error["code"] for error in self.errors(text, **kwargs)}

    def cli(self, *args, text=None):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), *args], input=text, text=True, capture_output=True, timeout=10)
        return result.returncode, json.loads(result.stdout)

    def test_local_verified_report(self):
        self.assertEqual(self.errors(BASE), [])

    def test_readonly_no_repair_identity_or_checks(self):
        text = no_changes(no_checks(BASE)).replace("| 请求类型 | 修复 |", "| 请求类型 | 仅诊断 |")
        text = text.replace("| 执行模式 | local_verify |", "| 执行模式 | 不适用：仅诊断 |")
        text = text.replace("example-1", "不适用：仅诊断").replace("digest-example", "不适用：未创建候选")
        text = text.replace("local_verified", "仅诊断，未执行修复")
        self.assertEqual(self.errors(text, checks="none"), [])

    def test_stop_before_candidate(self):
        text = no_changes(no_checks(BASE)).replace("local_verified", "inconclusive")
        text = text.replace("实际编辑前宿主记录 admission-1。", "stop；方向未决，见 admission-1。")
        self.assertEqual(self.errors(text, checks="none"), [])

    def test_stop_report_removes_inapplicable_checks_without_changing_requirement(self):
        text = no_changes(no_checks(BASE)).replace("local_verified", "denied")
        text = text.replace("本地编译和适用检查通过；未执行外层流水线。", "策略要求转人工，本轮无验证计划或实际验证。")
        table = """| 检查或命令 | 执行位置/目录或运行身份 | 结果 | 覆盖范围与未覆盖内容 | 证据 |
| --- | --- | --- | --- | --- |
| 原始 Job 失败 | 原 CI Job | failed | 历史失败，不是本轮验证 | error.log:10 |
| 候选验证 | 不适用 | 未执行：无候选 | 不适用，无本轮计划 | 停止决定 |

"""
        text = text.replace("## 6. 剩余问题与交接", table + "## 6. 剩余问题与交接")
        with tempfile.TemporaryDirectory(prefix="ci-report-check-") as directory:
            path = Path(directory) / "analysis-fix.md"
            path.write_text(text, encoding="utf-8")
            args = ("--report", str(path), "--classification", "none", "--checks", "none")
            code, invalid = self.cli(*args)
            self.assertEqual(code, 1)
            error = next(e for e in invalid["errors"] if e["code"] == "checks_none")
            self.assertEqual(text.splitlines()[error["line"] - 1], table.splitlines()[0])
            self.assertIn("保持 --checks none", error["message"])
            self.assertIn("验证计划或执行记录证明参数误传", error["message"])
            self.assertEqual(path.read_bytes(), text.encode())
            # Repair the document, not the already-correct requirement. Keep
            # the original failure evidence and the explanation of no checks.
            fixed = text.replace(table, "")
            path.write_text(fixed, encoding="utf-8")
            code, valid = self.cli(*args)
            self.assertEqual(code, 0)
            self.assertEqual(valid["format_status"], "valid")
            self.assertEqual(valid["sha256"], hashlib.sha256(fixed.encode()).hexdigest())
            self.assertNotEqual(valid["sha256"], invalid["sha256"])
            self.assertEqual(path.read_bytes(), fixed.encode())
            self.assertIn("原始失败：", fixed)
            self.assertIn("尚未形成验证计划，未运行检查", fixed)

    def test_required_but_incomplete_classification(self):
        text = classified().replace("C 编译 / C1 符号解析", "C 编译；小类未确定，未完成")
        text = text.replace("- 最终分类交付：已完成。", "- 最终分类交付：未完成：缺少区分小类的证据。")
        self.assertEqual(self.errors(text, classification="required"), [])

    def test_unrequested_classification_points_to_table_and_repairs_without_flag_change(self):
        text = classified()
        errors = [e for e in self.errors(text) if e["code"] == "classification_none"]
        self.assertEqual(len(errors), 1)
        header = "| 判断 | 分类路径（标准中的各级编号和名称） | 依据与未确定项 |"
        self.assertEqual(text.splitlines()[errors[0]["line"] - 1], header)
        self.assertIn("删除该表", errors[0]["message"])
        self.assertIn("不要仅为通过检查修改 --classification", errors[0]["message"])
        # A real corrective action: remove only the conditional table, keeping
        # the original requirement and all surrounding diagnostic evidence.
        lines = text.splitlines()
        start = errors[0]["line"] - 1
        end = start
        while end < len(lines) and lines[end].startswith("|"):
            end += 1
        fixed = "\n".join(lines[:start] + lines[end:]) + "\n"
        self.assertEqual(self.errors(fixed, classification="none"), [])

    def test_missing_initial_record_is_disclosed_not_fabricated(self):
        text = classified().replace("C 编译；小类未确定", "未知：初始记录缺失")
        self.assertEqual(self.errors(text, classification="required"), [])

    def test_classification_requirement_remains_when_standard_cannot_be_applied(self):
        for reason in ("固定入口不可读", "Profile 范围不适用", "配置版本冲突"):
            with self.subTest(reason=reason):
                text = classified().replace("C 编译；小类未确定", "未应用标准：" + reason)
                text = text.replace("C 编译 / C1 符号解析", "未应用标准：" + reason)
                text = text.replace("error.log:10；当时仅支持父类", "初次读取或核对失败，见 profile-read-1；" + reason)
                text = text.replace("src/Example.java:2；符合 C1 定义", "交付前仍未解决，见 profile-review-1；" + reason)
                text = text.replace("- 分类变化及原因：补取源码后确定小类。", "分类无变化，标准始终未能应用。")
                text = text.replace("- 最终分类交付：已完成。", "分类交付未完成：" + reason)
                self.assertEqual(self.errors(text, classification="required"), [])
                self.assertIn("classification_none", self.codes(text, classification="none"))
                without_table = re.sub(r"\n\| 判断 \|.*?\n\n", "\n", text, count=1, flags=re.DOTALL)
                self.assertIn("classification_required", self.codes(without_table, classification="required"))

    def test_unmatched_standard_is_reported_as_incomplete_not_omitted(self):
        text = classified().replace("C 编译 / C1 符号解析", "当前标准未匹配，最终分类未完成")
        text = text.replace("src/Example.java:2；符合 C1 定义", "已核对候选定义与排除条件，见 profile-review-2")
        text = text.replace("- 最终分类交付：已完成。", "分类交付未完成：当前标准未匹配。")
        self.assertEqual(self.errors(text, classification="required"), [])

    def test_requirement_cannot_be_satisfied_by_omitting_classification(self):
        self.assertIn("classification_required", self.codes(BASE, classification="required"))
        self.assertIn("required_table", self.codes(classified().replace("| 判断 |", "| 归类 |"), classification="required"))

    def test_classification_keeps_both_rows_but_allows_free_prose(self):
        text = classified().replace("| 初始分类 |", "| 最终分类 |")
        self.assertIn("classification_rows", self.codes(text, classification="required"))
        text = classified().replace("- 最终分类交付：已完成。", "分类已经完成；源码证据使首次仅支持父类的判断细化到 C1。")
        text = re.sub(r"^- Profile [^：]+：", "", text, flags=re.MULTILINE)
        self.assertEqual(self.errors(text, classification="required"), [])

    def test_metadata_can_be_extended_but_required_identity_stays(self):
        text = BASE.replace("| output_dir | /output |", "| output_dir | /output |\n| 生成时间 | 2026-09-28T08:00:00Z |\n| 运行场景 | CI Job |")
        self.assertEqual(self.errors(text), [])
        self.assertIn("metadata_fields", self.codes(text.replace("| invocation_id | example-1 |\n", "")))
        self.assertIn("metadata_duplicate", self.codes(text.replace("| invocation_id | example-1 |", "| invocation_id | example-1 |\n| invocation_id | example-2 |")))

    def test_bound_profile_is_required_in_metadata_even_when_body_records_it(self):
        text = classified()
        row = "| 绑定 Profile | example-profile；版本 v1；来源：可信调用参数 |\n"
        for replacement in ("", "| 执行者角色 | main |\n", "| Provider | example-provider |\n"):
            with self.subTest(replacement=replacement):
                self.assertIn("metadata_fields", self.codes(text.replace(row, replacement), classification="required"))

    def test_bound_profile_can_disclose_unapplied_or_per_object_bindings(self):
        for value in ("example-profile；版本未知；来源：固定绑定；未应用标准：入口不可读",
                      "未应用标准：固定绑定 example-a 与可信调用 example-b 冲突",
                      "example/build：example-a v1（固定绑定）；other/build：example-b v2（可信调用）"):
            with self.subTest(value=value):
                text = classified().replace("example-profile；版本 v1；来源：可信调用参数", value)
                self.assertEqual(self.errors(text, classification="required"), [])

    def test_body_accepts_narrative_lists_and_custom_subsections(self):
        text = re.sub(r"^- [^：\n]+：", "", BASE, flags=re.MULTILINE)
        text = text.replace("### 失败对象：example/build\n", "")
        text = text.replace("## 3. 目标行为与修复依据\n", "## 3. 目标行为与修复依据\n\n### 契约与取舍\n")
        text = text.replace("MR-1 描述及类型定义。", "先核对 MR-1 的描述，再查看类型定义，两份材料共同支持补充导入。")
        self.assertEqual(self.errors(text), [])

    def test_case_advice_preserves_prior_repair_failure_and_cleanup(self):
        text = BASE.replace("local_verified", "inconclusive")
        text = text.replace("本地编译和适用检查通过；未执行外层流水线。",
                            "候选编译失败后已恢复；现场材料不足以确认 SDK 兼容范围，转案例建议，本轮不再实施修复。")
        text = text.replace("缺少导入；参见 src/Example.java:2 和 MR-1 的契约说明。",
                            "补充导入后仍缺符号；SDK 与调用方的兼容范围仍待确认。")
        text = text.replace("当前候选仍符合依据；参见 review-1。", "候选已放弃并恢复；见 recovery-1。")
        text = text.replace("本次补充一处导入。", "本次曾补充导入，编译失败后已恢复原始内容。")
        text = text.replace("保留；见 diff-1", "已恢复；见 recovery-1")
        text = text.replace("编译与清理校验已完成。", "候选编译失败；恢复与清理校验已完成。")
        text = text.replace("| passed |", "| failed |")
        text = text.replace("当前根目录和 candidate baseline", "当前根目录及恢复前保留的原始内容")
        text = text.replace("当前候选绑定、match", "恢复后原始内容绑定、match")
        text = text.replace("| digest-example |", "| digest-example；已放弃并恢复，非交付候选 |")
        text = text.replace("宿主执行原定后续检查。", "建议先确认 SDK 兼容范围，实施前重新评估；建议尚未执行。")
        text = text.replace("源码留在工作空间，后续脚本负责提取 Patch。", "源码已恢复，保留候选失败与清理收据。")
        text = text.replace("## 3. 目标行为与修复依据\n", """## 3. 目标行为与修复依据

| 案例源 / 案例 | 来源与读取 | 适用性 |
| --- | --- | --- |
| example-a / C1 | [原文](https://wiki.example.invalid/cases/C1)，v2，2026-10-16T01:00:00Z，MCP 搜索后 CLI 读全文 | 历史 SDK 版本不同；建议核对版本约束与迁移记录，尚不能确认确定修法 |

""")
        text = text.replace("## 6. 剩余问题与交接\n", """## 6. 剩余问题与交接

查询目的为补充 SDK 兼容性排查方向。范围为 example-a 的已授权案例空间；
补查别名后没有新的区分线索，停止检索。另一已绑定来源 example-b 正文读取被拒，
未切换路径绕过，也不报告零结果；剩余兼容性缺口已列为建议前置条件。

""")
        self.assertEqual(self.errors(text), [])
        # A case-citation table cannot satisfy the separate requirement for a
        # declared verification table; this does not assess semantic correctness.
        self.assertIn("required_table", self.codes(no_checks(text)))
        self.assertIn("metadata_fields", self.codes(text.replace("| invocation_id | example-1 |\n", "")))

    def test_readonly_case_advice_keeps_future_checks_separate(self):
        text = no_changes(no_checks(BASE)).replace("| 请求类型 | 修复 |", "| 请求类型 | 仅诊断 |")
        text = text.replace("| 执行模式 | local_verify |", "| 执行模式 | 不适用：仅诊断 |")
        text = text.replace("example-1", "不适用：仅诊断").replace("digest-example", "不适用：未创建候选")
        text = text.replace("local_verified", "仅诊断，转案例建议，未执行修复")
        text = text.replace("本地编译和适用检查通过；未执行外层流水线。", "已查日志、源码及 MR，仍缺 SDK 兼容性依据；本轮只交付建议。")
        text = text.replace("缺少导入；参见 src/Example.java:2 和 MR-1 的契约说明。", "类型缺失机制尚未确认，仍缺 SDK 版本约束。")
        text = text.replace("补充导入，保持契约。", "建议先核对 SDK 版本与迁移说明，条件确认后再考虑调整依赖；尚未实施。")
        text = text.replace("实际编辑前宿主记录 admission-1。", "不适用：仅诊断，不建立修复准入。")
        text = text.replace("当前候选仍符合依据；参见 review-1。", "仅核对案例适用性，未创建候选。")
        text = text.replace("在修复准入前结束。", "仅诊断，未改动工作空间。")
        text = text.replace("## 6. 剩余问题与交接\n", """## 6. 剩余问题与交接

案例 example-a/C1 的 v2 原文来自 https://wiki.example.invalid/cases/C1；
历史环境不同，只提供迁移排查方向。以下均为后续建议，不是本次已声明验证计划。

| 建议检查 | 适用条件 | 状态 |
| --- | --- | --- |
| 编译及调用方兼容测试 | SDK 版本与目标行为确认、后续明确实施时 | 建议，未执行 |

""")
        text = text.replace("宿主执行原定后续检查。", "先补充版本和目标行为依据；不自动恢复自愈。")
        text = text.replace("源码留在工作空间，后续脚本负责提取 Patch。", "仅交付诊断与案例建议，没有候选或构建产物。")
        self.assertEqual(self.errors(text, checks="none"), [])
        # A future-check table must not stand in for checks actually declared for this call.
        self.assertIn("required_table", self.codes(text, checks="present"))

    def test_empty_chapters_still_fail(self):
        start = BASE.index("## 3. 目标行为与修复依据\n") + len("## 3. 目标行为与修复依据\n")
        end = BASE.index("## 4. 实际修改\n")
        for body in ("\n", "\n<!-- unfinished -->\n", "\n### 仅有小标题\n"):
            with self.subTest(body=body):
                self.assertIn("empty_section", self.codes(BASE[:start] + body + BASE[end:]))

    def test_modifications_can_use_prose_with_supplemental_comparison_table(self):
        before, section = BASE.split("## 4. 实际修改\n", 1)
        _, after = section.split("## 5. 验证结果\n", 1)
        text = before + """## 4. 实际修改

本次在 example/src/Example.java 补充导入，修改留在工作空间；见 diff-1。
本次生成的 target 已清理，原有文件未受影响，见 cleanup-1。

| 方案 | 影响 |
| --- | --- |
| 补充导入 | 恢复正确符号引用 |
| 改接口 | 超出已有契约 |

## 5. 验证结果
""" + after
        self.assertEqual(self.errors(text), [])

    def test_two_objects_with_mixed_requirements_and_multiple_repositories(self):
        start = BASE.index("### 失败对象：")
        end = BASE.index("## 3. 目标行为与修复依据")
        second = BASE[start:end].replace("example/build", "other/build")
        text = classified().replace("## 3. 目标行为与修复依据", second + "## 3. 目标行为与修复依据")
        text = text.replace("| example | /scene/source | abc123 | digest-example |", "| example | /scene/source | abc123 | digest-example |\n| other | /scene/other | xyz987 | 不适用：未创建候选 |")
        self.assertEqual(self.errors(text, classification="mixed"), [])
        self.assertIn("classification_required", self.codes(text, classification="required"))

    def test_remote_report_requires_candidate_run_binding(self):
        text = BASE.replace("local_verify", "remote_verify").replace("local_verified", "outer_ci_passed")
        self.assertIn("required_table", self.codes(text))
        text = text.replace("## 1. 处理结论", REMOTE + "## 1. 处理结论")
        self.assertEqual(self.errors(text), [])

    def test_planned_unexecuted_checks_still_use_table(self):
        text = BASE.replace("| passed |", "| 未执行：缺少编译器 |").replace("| match |", "| 未执行：等待构建 |")
        text = text.replace("编译与清理校验已完成。", "本次已计划编译与清理校验；缺少编译器，两项均未执行。")
        self.assertEqual(self.errors(text), [])
        self.assertIn("required_table", self.codes(no_checks(text), checks="present"))
        # Here a declared plan really exists: correcting a mistaken flag is
        # allowed. The structural checker still does not adjudicate that fact.
        code, invalid = self.cli("--report", "-", "--classification", "none", "--checks", "none", text=text)
        self.assertEqual(code, 1)
        self.assertIn("checks_none", {e["code"] for e in invalid["errors"]})
        code, valid = self.cli("--report", "-", "--classification", "none", "--checks", "present", text=text)
        self.assertEqual(code, 0)
        self.assertEqual(valid["format_status"], "valid")
        self.assertEqual(valid["sha256"], invalid["sha256"])

    def test_verification_can_be_split_into_multiple_bound_tables(self):
        text = split_checks()
        self.assertEqual(self.errors(text), [])
        code, result = self.cli("--report", "-", "--classification", "none", "--checks", "present", text=text)
        self.assertEqual((code, result["format_status"]), (0, "valid"))
        self.assertIn("checks_none", self.codes(text, checks="none"))

    def test_every_split_verification_table_must_have_rows_and_consistent_width(self):
        text = split_checks()
        empty_second = re.sub(r"^\| workspace_check check \|.*\n", "", text, flags=re.MULTILINE)
        self.assertIn("empty_table", self.codes(empty_second))
        extra_cell = text.replace("| workspace_check check |", "| workspace_check check | unexpected |")
        self.assertIn("table_width", self.codes(extra_cell))
        first, second = text.split("### 工作空间校验：", 1)
        wrong_header = first + "### 工作空间校验：" + second.replace("| 覆盖范围与未覆盖内容 |", "| 范围 |")
        self.assertIn("verification_table", self.codes(wrong_header))

    def test_split_checks_can_coexist_with_supplemental_comparison_table(self):
        text = split_checks().replace("## 6. 剩余问题与交接", "| 验证路径 | 局限 |\n| --- | --- |\n| 本地 | 未覆盖外层 CI |\n\n## 6. 剩余问题与交接")
        self.assertEqual(self.errors(text), [])

    def test_multiple_checks_do_not_relax_classification_table_cardinality(self):
        text = classified(split_checks())
        table = re.search(r"\n\| 判断 \|.*?\n\n", text, re.DOTALL).group(0)
        text = text.replace(table, table + table)
        self.assertIn("classification_group", self.codes(text, classification="required"))

    def test_missing_reordered_duplicate_or_wrong_level_sections(self):
        for text in (
            BASE.replace("## 4. 实际修改", "### 4. 实际修改"),
            BASE.replace("## 4. 实际修改", "## 修改"),
            BASE.replace("## 3. 目标行为与修复依据", "## 4. 实际修改"),
            BASE + "\n## 1. 处理结论\n多余章节\n",
        ):
            with self.subTest(text=text[-60:]):
                self.assertIn("headings", self.codes(text))

    def test_fenced_evidence_cannot_supply_required_verification_table(self):
        text = BASE.replace("| 检查或命令 |", "```text\n| 检查或命令 |")
        text = text.replace("- 工作空间校验：", "```\n\n- 工作空间校验：")
        self.assertIn("required_table", self.codes(text))

    def test_headings_in_fenced_logs_are_not_sections(self):
        text = BASE.replace("## 3. 目标行为与修复依据", "~~~~text\n## unexpected\n```\n# fake title\n~~~~\n\n## 3. 目标行为与修复依据")
        self.assertEqual(self.errors(text), [])

    def test_commented_metadata_does_not_count(self):
        text = BASE.replace("| invocation_id | example-1 |", "<!-- | invocation_id | example-1 | -->")
        self.assertIn("metadata_fields", self.codes(text))

    def test_unfinished_template_and_unclosed_fences_are_rejected(self):
        self.assertIn("placeholder", self.codes(BASE.replace("example-1", "{{REPORT:调用身份}}")))
        self.assertIn("unclosed_fence", self.codes(BASE + "\n```text\nunfinished\n"))

    def test_wrong_columns_empty_cells_and_unescaped_pipes(self):
        self.assertIn("required_table", self.codes(BASE.replace("| 检查或命令 |", "| 命令 |")))
        self.assertIn("empty_cell", self.codes(BASE.replace("| invocation_id | example-1 |", "| invocation_id | |")))
        self.assertIn("table_width", self.codes(BASE.replace("mvn compile", "`mvn compile | tee log`")))
        self.assertEqual(self.errors(BASE.replace("mvn compile", r"`mvn compile \| tee log`")), [])

    def test_structure_pass_is_not_a_semantic_verdict(self):
        # Unsupported claims cannot be adjudicated from Markdown structure.
        text = BASE.replace("passed", "failed")
        self.assertEqual(self.errors(text), [])

    def test_stdin_fallback_no_output_file(self):
        text = BASE.replace("源码留在工作空间，后续脚本负责提取 Patch。", "输出目录不可写；本次通过完整正文交付，未生成报告文件。")
        code, result = self.cli("--report", "-", "--classification", "none", "--checks", "present", text=text)
        self.assertEqual(code, 0)
        self.assertEqual(result["format_status"], "valid")
        self.assertEqual(result["sha256"], hashlib.sha256(text.encode()).hexdigest())

    def test_crlf_and_bom_file_are_read_without_rewriting(self):
        with tempfile.TemporaryDirectory(prefix="ci-report-check-") as directory:
            path = Path(directory) / "analysis-fix.md"
            raw = b"\xef\xbb\xbf" + BASE.replace("\n", "\r\n").encode()
            path.write_bytes(raw)
            code, result = self.cli("--report", str(path), "--classification", "none", "--checks", "present")
            self.assertEqual(code, 0)
            self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(path.read_bytes(), raw)

    def test_file_check_is_readonly_and_checks_filename(self):
        with tempfile.TemporaryDirectory(prefix="ci-report-check-") as directory:
            path = Path(directory) / "analysis-fix.md"
            path.write_text(BASE, encoding="utf-8")
            before = path.read_bytes()
            code, result = self.cli("--report", str(path), "--classification", "none", "--checks", "present")
            self.assertEqual((code, result["format_status"]), (0, "valid"))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(directory).iterdir()), [path])
            wrong = path.with_name("analysis.md")
            path.rename(wrong)
            code, result = self.cli("--report", str(wrong), "--classification", "none", "--checks", "present")
            self.assertEqual(code, 1)
            self.assertIn("filename", {e["code"] for e in result["errors"]})
            self.assertEqual(wrong.read_bytes(), before)

    def test_input_errors_are_incomplete(self):
        with tempfile.TemporaryDirectory(prefix="ci-report-check-") as directory:
            path = Path(directory) / "analysis-fix.md"
            for content in (None, b"\xff", b"x" * (report.MAX_BYTES + 1)):
                if content is not None:
                    path.write_bytes(content)
                code, result = self.cli("--report", str(path), "--classification", "none", "--checks", "none")
                self.assertEqual((code, result["format_status"]), (2, "incomplete"))

    def test_final_report_check_and_readback_can_share_one_readonly_batch(self):
        # Conditions come from the task; each completed report passes on first check.
        stop = no_changes(no_checks(BASE)).replace("local_verified", "inconclusive")
        required_stop = classified(stop).replace("C 编译 / C1 符号解析", "未应用标准：入口不可读")
        remote = BASE.replace("local_verify", "remote_verify").replace("local_verified", "outer_ci_passed")
        remote = remote.replace("## 1. 处理结论", REMOTE + "## 1. 处理结论")
        planned = BASE.replace("| passed |", "| 未执行：缺少编译器 |")
        for text, classification, checks in ((stop, "none", "none"), (required_stop, "required", "none"),
                                              (planned, "none", "present"), (remote, "none", "present")):
            with self.subTest(classification=classification, checks=checks), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "analysis-fix.md"
                raw = text.encode("utf-8")
                path.write_bytes(raw)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    check = pool.submit(self.cli, "--report", str(path), "--classification", classification, "--checks", checks)
                    readback = pool.submit(path.read_bytes)
                    code, receipt = check.result()
                    returned = readback.result()
                self.assertEqual((code, receipt["format_status"]), (0, "valid"))
                self.assertEqual(receipt["sha256"], hashlib.sha256(returned).hexdigest())
                self.assertEqual(returned, raw)
                self.assertEqual(path.read_bytes(), raw)
                self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_report_edit_invalidates_previous_check_and_readback_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analysis-fix.md"
            path.write_text(BASE, encoding="utf-8")
            _, old = self.cli("--report", str(path), "--classification", "none", "--checks", "present")
            old_readback = path.read_bytes()
            # A well-formed new body still needs its own receipt and readback.
            path.write_text(BASE + "\n补充交接证据：handoff-2。\n", encoding="utf-8")
            self.assertNotEqual(hashlib.sha256(path.read_bytes()).hexdigest(), old["sha256"])
            self.assertNotEqual(path.read_bytes(), old_readback)
            code, current = self.cli("--report", str(path), "--classification", "none", "--checks", "present")
            self.assertEqual((code, current["format_status"]), (0, "valid"))
            self.assertEqual(current["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
