"""Deterministic tests for tools/verify.py (injected executor seam).

The tests never execute the real canonical gates: an injected executor
records the command lines and returns controlled results, so the
verifier contract itself (exact gates, sys.executable, deterministic
order, first-failure stop, surfaced output, non-zero failure) is proven
without recursively running the project suite.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "verify.py"
SPEC = importlib.util.spec_from_file_location("hermes_verify_tool", TOOL_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load verification entrypoint from {TOOL_PATH}")
verify_tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verify_tool
SPEC.loader.exec_module(verify_tool)


class VerifyEntrypointTests(unittest.TestCase):
    maxDiff = None

    def test_canonical_checks_are_exactly_the_documented_gates(self) -> None:
        self.assertEqual(
            list(verify_tool.CHECKS),
            [
                ("-m", "pytest", "-q"),
                ("-m", "compileall", "src"),
                ("-m", "ruff", "check", "."),
                ("-m", "mypy", "src"),
                ("-m", "pip", "check"),
            ],
        )

    def test_gates_use_sys_executable_in_exact_order_and_exit_zero(self) -> None:
        ran: list[list[str]] = []

        def executor(argv: list[str]) -> tuple[int, str]:
            ran.append(argv)
            return 0, "quiet output\n"

        with redirect_stdout(io.StringIO()) as captured:
            code = verify_tool.verify(executor=executor)

        self.assertEqual(code, 0)
        self.assertEqual(ran, [[sys.executable, *check] for check in verify_tool.CHECKS])
        self.assertIn("[verify] all canonical checks passed", captured.getvalue())

    def test_first_failure_stops_later_gates_and_exits_non_zero(self) -> None:
        ran: list[list[str]] = []

        def executor(argv: list[str]) -> tuple[int, str]:
            ran.append(argv)
            if len(ran) == 1:
                return 3, "failing gate output\n"
            return 0, "never reached\n"

        with redirect_stdout(io.StringIO()) as captured:
            code = verify_tool.verify(executor=executor)

        self.assertEqual(code, 1)
        self.assertEqual(len(ran), 1)
        self.assertIn("failing gate output", captured.getvalue())
        self.assertIn("FAILED", captured.getvalue())
        self.assertNotIn("never reached", captured.getvalue())

    def test_process_start_failure_is_a_verification_failure(self) -> None:
        def executor(argv: list[str]) -> tuple[int, str]:
            return 127, "failed to start\n"

        with redirect_stdout(io.StringIO()) as captured:
            code = verify_tool.verify(executor=executor)

        self.assertEqual(code, 1)
        self.assertIn("failed to start", captured.getvalue())

    def test_real_executor_surfaces_gate_output_and_exit_code(self) -> None:
        probe = (
            "import sys; print('gate stdout'); "
            "print('gate stderr', file=sys.stderr); sys.exit(7)"
        )
        code, output = verify_tool.real_executor([sys.executable, "-c", probe])
        self.assertEqual(code, 7)
        self.assertIn("gate stdout", output)
        self.assertIn("gate stderr", output)

    def test_real_executor_reports_start_failure_as_non_zero(self) -> None:
        missing = str(Path("definitely") / "missing" / "interpreter")
        code, output = verify_tool.real_executor([missing, "-c", "pass"])
        self.assertNotEqual(code, 0)
        self.assertTrue(output.strip())


if __name__ == "__main__":
    unittest.main()
