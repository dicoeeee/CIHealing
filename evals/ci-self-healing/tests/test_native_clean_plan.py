"""Language-neutral clean plans: no tool inference, fallback, or global writes."""
from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import EvalError, read_json, run_logged, write_json, write_new
from host_sandbox import policy
from native_clean import NativeCleanPlan, clean_build
from resource_cleanup import OwnedResources, validate_cleanup


class NativeCleanPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="generic-clean-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def fake(self, argv, directory, timeout, env, cwd):
        write_new(Path(directory) / "stdout.log", "synthetic clean command")
        process = {"status": "completed", "returncode": 0, "cleanup": {"status": "confirmed"}}
        write_json(Path(directory) / "process.json", process)
        return process

    def test_plans_forward_exact_tool_module_configuration_and_environment(self):
        commands = {
            "cmake": ("/tools/cmake", "--build", "build debug", "--config", "Debug", "--target", "clean"),
            "make": ("/usr/bin/make", "-C", "component", "clean"),
            "gradle": ("/tools/gradle", "--offline", ":module:clean"),
            "cargo": ("/tools/cargo", "clean", "--offline", "--target-dir", "private-target"),
            "go": ("/tools/go", "clean", "./component"),
            "dotnet": ("/tools/dotnet", "clean", "project.csproj", "--configuration", "Release"),
            "npm": ("/tools/npm", "run", "clean", "--workspace", "package-a"),
            "python-project-task": (sys.executable, "-B", "tasks.py", "clean"),
            "custom-build-system": ("/bin/sh", "scripts/remove-build-products.sh"),
        }
        for name, argv in commands.items():
            with self.subTest(system=name), OwnedResources(self.root, name) as resources:
                workspace = resources.mkdir(name + "-workspace")
                write_new(workspace / "leftover.unknown", "host must still reclaim this")
                plan = NativeCleanPlan(name, argv, workspace, {"PATH": "/tools:/usr/bin:/bin", "BUILD_MODE": "local"})
                with mock.patch("native_clean.run_logged", side_effect=self.fake) as run:
                    result = clean_build({}, workspace, self.root / (name + "-clean"), resources, plan=plan)
                self.assertEqual(result["build_system"], name)
                self.assertEqual(run.call_args.args[0][3:], list(argv))
                self.assertEqual(run.call_args.kwargs["cwd"], workspace)
                self.assertEqual(run.call_args.kwargs["env"], plan.environment)
                self.assertTrue((workspace / "leftover.unknown").exists())
                self.assertEqual(validate_cleanup(resources.finish()), "cleaned")

    def test_no_verified_plan_skips_without_maven_even_if_pom_exists(self):
        with OwnedResources(self.root, "unknown") as resources:
            workspace = resources.mkdir("workspace")
            write_new(workspace / "pom.xml", "not proof Maven was used")
            with mock.patch("native_clean.run_logged") as run:
                result = clean_build({}, workspace, self.root / "clean", resources)
            self.assertEqual(result["reason"], "no_verified_native_clean_plan")
            run.assert_not_called()
            self.assertEqual(resources.finish()["status"], "cleaned")

    def test_invalid_or_outside_plans_never_execute(self):
        with OwnedResources(self.root, "invalid") as resources:
            workspace = resources.mkdir("workspace")
            plan = NativeCleanPlan("project", ("/usr/bin/make", "clean"), workspace, {})
            for invalid in (replace(plan, argv="make clean"), replace(plan, argv=("make", "clean")),
                            replace(plan, cwd=self.root), replace(plan, timeout_seconds=0),
                            replace(plan, required_files=(self.root / "outside",))):
                with self.subTest(plan=invalid), mock.patch("native_clean.run_logged") as run:
                    with self.assertRaises(EvalError):
                        clean_build({}, workspace, self.root / "clean", resources, plan=invalid)
                    run.assert_not_called()

    def test_missing_offline_prerequisite_does_not_install_or_switch_tools(self):
        with OwnedResources(self.root, "offline") as resources:
            workspace = resources.mkdir("workspace")
            plan = NativeCleanPlan("project-tool", ("/tools/cleaner", "clean"), workspace, {},
                                   required_files=(workspace / "tool-distribution",))
            with mock.patch("native_clean.run_logged") as run:
                result = clean_build({}, workspace, self.root / "clean", resources, plan=plan)
            self.assertEqual(result["reason"], "offline_clean_prerequisite_missing")
            self.assertEqual(result["build_system"], "project-tool")
            run.assert_not_called()

    def test_generic_sandbox_does_not_require_java_and_never_allows_network(self):
        text = policy({"build_tools": ["/opt/tool/bin/compiler"]}, write=[self.root])
        self.assertIn('(deny network*)', text)
        self.assertNotIn('network-outbound', text)
        self.assertIn('/opt/tool', text)
        self.assertNotIn('maven', text)
        with self.assertRaises(EvalError):
            policy({"build_tools": [str(Path.home() / "bin/tool")]})


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("CI_CLEANUP_TEST_NON_JAVA") == "1",
                     "explicit local sandbox tool test required")
class NonJavaOfflineCleanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="non-java-clean-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        write_new(self.root / "original.txt", "keep original")
        self.env = {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}

    def test_actual_c_make_build_clean_and_residue_reclamation(self):
        make = Path('/Library/Developer/CommandLineTools/usr/bin/make')
        cc = Path('/Library/Developer/CommandLineTools/usr/bin/clang')
        self.assertTrue(make.is_file() and cc.is_file(), "local C toolchain required")
        config = {"build_tools": [str(make), str(cc)]}
        with OwnedResources(self.root, "make", evidence=("original.txt", "build", "clean", "inputs")) as resources:
            workspace = resources.mkdir("workspace")
            source = 'int answer(void) { return 42; }\n'
            makefile = f'all: unit.o\nunit.o: unit.c\n\t{cc} -c unit.c -o unit.o\nclean:\n\trm -f unit.o\n'
            for name, data in (("unit.c", source), ("Makefile", makefile)):
                write_new(workspace / name, data)
                write_new(self.root / "inputs" / name, data)
            sandbox = resources.directory / "build.sb"
            write_new(sandbox, policy(config, write=[workspace]))
            env = {**self.env, "TMPDIR": str(workspace), "DEVELOPER_DIR": "/Library/Developer/CommandLineTools"}
            resources.watch(self.root / "build/process.json")
            build = run_logged(["/usr/bin/sandbox-exec", "-f", str(sandbox), str(make), "all"],
                               self.root / "build", 30, cwd=workspace, env=env)
            self.assertEqual(build["returncode"], 0, (self.root / "build/stderr.log").read_text())
            self.assertTrue((workspace / "unit.o").exists())
            write_new(workspace / ".extra-temp", "not covered by make clean")
            result = clean_build(config, workspace, self.root / "clean", resources,
                                 plan=NativeCleanPlan("make", (str(make), "clean"), workspace, env))
            self.assertEqual(result["status"], "completed", result)
            self.assertFalse((workspace / "unit.o").exists())
            self.assertTrue((workspace / ".extra-temp").exists())
            self.assertEqual(validate_cleanup(resources.finish()), "cleaned")
            self.assertFalse(workspace.exists())
        self.assertEqual((self.root / "original.txt").read_text(), "keep original")

    def test_actual_python_project_clean_task_removes_bytecode(self):
        config = {"python": sys.executable}
        with OwnedResources(self.root, "python", evidence=("original.txt", "build", "clean", "inputs")) as resources:
            workspace = resources.mkdir("workspace")
            for name, content in (("module.py", "answer = 42\n"),
                                  ("clean_task.py", "from pathlib import Path\nPath('module.pyc').unlink()\n")):
                write_new(workspace / name, content)
                write_new(self.root / "inputs" / name, content)
            sandbox = resources.directory / "build.sb"
            write_new(sandbox, policy(config, write=[workspace]))
            env = {**self.env, "TMPDIR": str(workspace)}
            resources.watch(self.root / "build/process.json")
            build = run_logged(["/usr/bin/sandbox-exec", "-f", str(sandbox), sys.executable, "-B", "-c",
                                "import py_compile; py_compile.compile('module.py', cfile='module.pyc', doraise=True)"],
                               self.root / "build", 30, cwd=workspace, env=env)
            self.assertEqual(build["returncode"], 0)
            self.assertTrue((workspace / "module.pyc").exists())
            result = clean_build(config, workspace, self.root / "clean", resources,
                                 plan=NativeCleanPlan("python-project-task", (sys.executable, "-B", "clean_task.py"), workspace, env))
            self.assertEqual(result["status"], "completed", result)
            self.assertFalse((workspace / "module.pyc").exists())
            self.assertEqual(validate_cleanup(resources.finish()), "cleaned")

    def test_project_clean_cannot_remove_original_outside_disposable_scope(self):
        config = {"python": sys.executable}
        with OwnedResources(self.root, "escape", evidence=("original.txt", "clean")) as resources:
            workspace = resources.mkdir("workspace")
            write_new(workspace / "clean.py", "from pathlib import Path\nPath('../original.txt').unlink()\n")
            result = clean_build(config, workspace, self.root / "clean", resources,
                                 plan=NativeCleanPlan("project-task", (sys.executable, "-B", "clean.py"), workspace, self.env))
            self.assertEqual(result["status"], "failed")
            self.assertEqual((self.root / "original.txt").read_text(), "keep original")
            self.assertEqual(validate_cleanup(resources.finish()), "cleaned")


if __name__ == "__main__":
    unittest.main()
