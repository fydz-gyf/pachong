from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import unified_scraper


REPO_ROOT = Path(__file__).resolve().parents[1]


class UnifiedScraperTests(unittest.TestCase):
    def test_unified_batch_launcher_is_ascii_and_preserves_contract(self) -> None:
        batch_path = REPO_ROOT / "start_scraper.bat"
        self.assertTrue(batch_path.is_file(), batch_path)

        raw = batch_path.read_bytes()
        text = raw.decode("ascii")
        self.assertEqual(text.encode("ascii"), raw)
        for required in (
            "%*",
            "%ERRORLEVEL%",
            "where python",
            "where py",
            "pause",
            "exit /b %EXIT_CODE%",
            "unified_scraper.py",
        ):
            self.assertIn(required, text)

    def test_all_tracked_batch_launchers_are_ascii(self) -> None:
        batch_paths = sorted(
            path
            for path in REPO_ROOT.rglob("*.bat")
            if ".git" not in path.parts
        )
        self.assertGreaterEqual(len(batch_paths), 4)
        for batch_path in batch_paths:
            with self.subTest(batch_path=batch_path.relative_to(REPO_ROOT)):
                raw = batch_path.read_bytes()
                self.assertEqual(raw.decode("ascii").encode("ascii"), raw)

    def test_wayfair_batch_uses_relative_project_paths(self) -> None:
        batch_path = REPO_ROOT / "wayfair" / "启动Wayfair类目采集.bat"
        text = batch_path.read_text(encoding="ascii")
        self.assertIn('set "PROJECT_DIR=%~dp0"', text)
        self.assertIn('set "IMAGE_TOOL_DIR=%~dp0"', text)
        self.assertNotIn(r"E:\wayfair-local-scraper", text)
        self.assertNotIn(r"E:\wayfair-image-embedder", text)

    @unittest.skipUnless(os.name == "nt", "the BAT parser is only available on Windows")
    def test_unified_batch_dry_run_does_not_start_scraper(self) -> None:
        batch_path = REPO_ROOT / "start_scraper.bat"
        result = subprocess.run(
            [
                "cmd.exe",
                "/d",
                "/c",
                "call",
                str(batch_path),
                "--platform",
                "taobao",
                "--dry-run",
            ],
            input="\r\n",
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DRY-RUN", result.stdout)

    def make_projects(self, *, walmart_venv: bool = True) -> unified_scraper.ProjectPaths:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        taobao = root / "淘宝 项目"
        walmart = root / "Walmart 项目"
        wayfair = root / "Wayfair 项目"
        (taobao).mkdir()
        if walmart_venv:
            (walmart / ".venv" / "Scripts").mkdir(parents=True)
        else:
            walmart.mkdir()
        (wayfair).mkdir()
        (taobao / "taobao_scraper.py").write_text("# test\n", encoding="utf-8")
        (walmart / "run.py").write_text("# test\n", encoding="utf-8")
        if walmart_venv:
            (walmart / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
        (wayfair / "启动Wayfair类目采集.bat").write_text("@echo off\n", encoding="utf-8")
        return unified_scraper.ProjectPaths(taobao, walmart, wayfair)

    def tearDown(self) -> None:
        temporary = getattr(self, "temp_dir", None)
        if temporary is not None:
            temporary.cleanup()

    def test_normalize_platform_and_exit_aliases(self) -> None:
        self.assertEqual(unified_scraper.normalize_platform("淘宝"), "taobao")
        self.assertEqual(unified_scraper.normalize_platform("2"), "walmart")
        self.assertEqual(unified_scraper.normalize_platform("WAYFAIR"), "wayfair")
        self.assertIsNone(unified_scraper.normalize_platform("0"))
        with self.assertRaises(ValueError):
            unified_scraper.normalize_platform("amazon")

    def test_repo_defaults_point_to_monorepo_platform_directories(self) -> None:
        paths = unified_scraper.load_project_paths(
            launcher_dir=REPO_ROOT,
            environ={},
        )
        self.assertEqual(paths.taobao, (REPO_ROOT / "taobao").resolve())
        self.assertEqual(paths.walmart, (REPO_ROOT / "walmart").resolve())
        self.assertEqual(paths.wayfair, (REPO_ROOT / "wayfair").resolve())

    def test_menu_retries_invalid_choice_and_can_exit(self) -> None:
        output: list[str] = []
        answers = iter(["bad", "3"])
        selected = unified_scraper.choose_platform(lambda _prompt: next(answers), output.append)
        self.assertEqual(selected, "wayfair")
        self.assertTrue(any("不支持的平台" in item for item in output))

        self.assertIsNone(
            unified_scraper.choose_platform(lambda _prompt: "0", output.append)
        )

    def test_build_commands_use_each_project_and_preserve_interactive_flow(self) -> None:
        paths = self.make_projects()
        python = Path(self.temp_dir.name) / "current python.exe"
        cmd = Path(self.temp_dir.name) / "cmd.exe"
        python.write_bytes(b"")
        cmd.write_bytes(b"")

        taobao = unified_scraper.build_launch_plan(
            "taobao", paths, python_executable=python, command_interpreter=cmd
        )
        self.assertEqual(taobao.cwd, paths.taobao)
        self.assertEqual(
            taobao.command,
            (str(python), str(paths.taobao / "taobao_scraper.py"), "--interactive"),
        )

        walmart = unified_scraper.build_launch_plan("walmart", paths)
        self.assertEqual(walmart.cwd, paths.walmart)
        self.assertEqual(walmart.command[-2:], ("--browser", "adspower"))
        self.assertEqual(walmart.command[1], str(paths.walmart / "run.py"))
        self.assertEqual(
            walmart.runtime_executable,
            paths.walmart / ".venv" / "Scripts" / "python.exe",
        )
        self.assertEqual(walmart.command[0], str(walmart.runtime_executable))

        wayfair = unified_scraper.build_launch_plan(
            "wayfair", paths, command_interpreter=cmd
        )
        self.assertEqual(wayfair.cwd, paths.wayfair)
        self.assertEqual(wayfair.command[0], str(cmd))
        self.assertEqual(wayfair.command[1:3], ("/d", "/c"))
        self.assertIn(str(paths.wayfair / "启动Wayfair类目采集.bat"), wayfair.command[3])

    def test_preflight_and_dry_run_do_not_start_a_child(self) -> None:
        paths = self.make_projects()
        fake_python = Path(self.temp_dir.name) / "python.exe"
        fake_python.write_bytes(b"")
        plan = unified_scraper.build_launch_plan(
            "taobao", paths, python_executable=fake_python
        )
        unified_scraper.preflight(plan)

        output: list[str] = []
        exit_code = unified_scraper.main(
            [
                "--platform",
                "taobao",
                "--dry-run",
                "--taobao-dir",
                str(paths.taobao),
                "--taobao-python",
                str(fake_python),
            ],
            output_fn=output.append,
            error_fn=output.append,
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue(any("DRY-RUN" in item for item in output))

    def test_walmart_falls_back_to_launcher_python_without_local_venv(self) -> None:
        paths = self.make_projects(walmart_venv=False)
        launcher_python = Path(self.temp_dir.name) / "launcher python.exe"
        launcher_python.write_bytes(b"")

        with patch.object(unified_scraper.sys, "executable", str(launcher_python)):
            plan = unified_scraper.build_launch_plan("walmart", paths)

        self.assertEqual(plan.runtime_executable, launcher_python)
        self.assertEqual(plan.command[0], str(launcher_python))
        unified_scraper.preflight(plan)

    def test_dry_run_for_all_platforms_does_not_crawl_or_spawn(self) -> None:
        paths = self.make_projects()
        fake_python = Path(self.temp_dir.name) / "python.exe"
        fake_cmd = Path(self.temp_dir.name) / "cmd.exe"
        fake_python.write_bytes(b"")
        fake_cmd.write_bytes(b"")

        cases = (
            (
                "taobao",
                "--taobao-dir",
                paths.taobao,
                "--taobao-python",
                fake_python,
            ),
            ("walmart", "--walmart-dir", paths.walmart),
            (
                "wayfair",
                "--wayfair-dir",
                paths.wayfair,
                "--cmd-exe",
                fake_cmd,
            ),
        )
        for case in cases:
            platform, *overrides = case
            argv = ["--platform", platform, "--dry-run"]
            argv.extend(str(value) for value in overrides)
            output: list[str] = []
            with self.subTest(platform=platform):
                exit_code = unified_scraper.main(
                    argv,
                    output_fn=output.append,
                    error_fn=output.append,
                )
                self.assertEqual(exit_code, 0, output)
                self.assertTrue(any("DRY-RUN" in item for item in output))

    def test_execute_plan_propagates_exit_code_and_disables_shell(self) -> None:
        paths = self.make_projects()
        fake_python = Path(self.temp_dir.name) / "python.exe"
        fake_python.write_bytes(b"")
        plan = unified_scraper.build_launch_plan(
            "taobao", paths, python_executable=fake_python
        )
        calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

        def fake_runner(command, **kwargs):
            calls.append((tuple(command), kwargs))
            return SimpleNamespace(returncode=17)

        code = unified_scraper.execute_plan(
            plan,
            runner=fake_runner,
            environ={"PATH": "test"},
        )
        self.assertEqual(code, 17)
        self.assertEqual(calls[0][0], plan.command)
        self.assertFalse(calls[0][1]["shell"])
        self.assertEqual(calls[0][1]["cwd"], str(paths.taobao))
        self.assertEqual(calls[0][1]["env"]["PYTHONUTF8"], "1")
        self.assertEqual(calls[0][1]["env"]["PYTHONIOENCODING"], "utf-8")


if __name__ == "__main__":
    unittest.main()
