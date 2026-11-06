"""Keep the archived cleanup implementation testable without shipping it."""
import importlib.util
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "legacy/cleanup-v7/skill/tests/test_workspace_check.py"
spec = importlib.util.spec_from_file_location("legacy_workspace_tests", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
WorkspaceCheckTests = module.WorkspaceCheckTests
