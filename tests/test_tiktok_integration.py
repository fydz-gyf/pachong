from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import unified_scraper


REPO_ROOT = Path(__file__).resolve().parents[1]


class TikTokIntegrationTests(unittest.TestCase):
    def test_tiktok_aliases_and_menu(self) -> None:
        self.assertEqual(unified_scraper.normalize_platform("5"), "tiktok")
        self.assertEqual(unified_scraper.normalize_platform("TikTok"), "tiktok")
        self.assertEqual(unified_scraper.normalize_platform("tt"), "tiktok")

        output: list[str] = []
        selected = unified_scraper.choose_platform(lambda _prompt: "5", output.append)
        self.assertEqual(selected, "tiktok")
        self.assertTrue(any("5. TikTok" in item for item in output))

    def test_tiktok_directory_default_and_environment_override(self) -> None:
        paths = unified_scraper.load_project_paths(
            launcher_dir=REPO_ROOT,
            environ={},
        )
        self.assertEqual(paths.tiktok, (REPO_ROOT / "tiktok").resolve())

        overridden = unified_scraper.load_project_paths(
            launcher_dir=REPO_ROOT,
            environ={"TIKTOK_SCRAPER_DIR": "D:/tiktok-local"},
        )
        self.assertEqual(overridden.tiktok, Path("D:/tiktok-local").resolve())

    def make_project(self) -> tuple[Path, Path, Path]:
        temporary = tempfile.TemporaryDirectory()
        self.temp_dir = temporary
        root = Path(temporary.name)
        project = root / "TikTok project"
        project.mkdir()
        (project / "tiktok_collector.py").write_text("# test\n", encoding="utf-8")
        (project / "keywords.txt").write_text("safe-keyword\n", encoding="utf-8")
        fake_python = root / "python.exe"
        fake_python.write_bytes(b"")
        return root, project, fake_python

    def tearDown(self) -> None:
        temporary = getattr(self, "temp_dir", None)
        if temporary is not None:
            temporary.cleanup()

    def test_tiktok_command_preserves_defaults_and_is_path_relative(self) -> None:
        _root, project, fake_python = self.make_project()
        plan = unified_scraper.build_launch_plan(
            "tt",
            unified_scraper.ProjectPaths(
                REPO_ROOT / "taobao",
                REPO_ROOT / "walmart",
                REPO_ROOT / "wayfair",
                REPO_ROOT / "reddit",
                project,
            ),
            tiktok_python_executable=fake_python,
        )
        self.assertEqual(plan.platform, "tiktok")
        self.assertEqual(plan.cwd, project)
        self.assertEqual(plan.entrypoint, project / "tiktok_collector.py")
        self.assertEqual(plan.runtime_executable, fake_python)
        self.assertEqual(
            plan.command,
            (
                str(fake_python),
                str(project / "tiktok_collector.py"),
                "--keywords",
                str(project / "keywords.txt"),
                "--max-videos",
                "10",
                "--search-scrolls",
                "20",
                "--comment-scrolls",
                "80",
                "--expand-replies",
            ),
        )

    def test_tiktok_runtime_prefers_local_root_then_current_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "tiktok"
            project.mkdir()
            local = project / ".venv" / "Scripts" / "python.exe"
            local.parent.mkdir(parents=True)
            local.write_bytes(b"")
            self.assertEqual(unified_scraper._tiktok_python(project, root), local)

            local.unlink()
            repo_python = root / "repo" / ".venv" / "Scripts" / "python.exe"
            repo_python.parent.mkdir(parents=True)
            repo_python.write_bytes(b"")
            self.assertEqual(
                unified_scraper._tiktok_python(project, root / "repo"), repo_python
            )

            repo_python.unlink()
            fallback = root / "launcher python.exe"
            fallback.write_bytes(b"")
            with patch.object(unified_scraper.sys, "executable", str(fallback)):
                self.assertEqual(
                    unified_scraper._tiktok_python(project, root / "repo"), fallback
                )

    def test_tiktok_preflight_and_dry_run_do_not_spawn_or_network(self) -> None:
        _root, project, fake_python = self.make_project()
        paths = unified_scraper.ProjectPaths(
            REPO_ROOT / "taobao",
            REPO_ROOT / "walmart",
            REPO_ROOT / "wayfair",
            REPO_ROOT / "reddit",
            project,
        )
        plan = unified_scraper.build_launch_plan(
            "tiktok",
            paths,
            tiktok_python_executable=fake_python,
        )
        unified_scraper.preflight(plan)

        output: list[str] = []
        exit_code = unified_scraper.main(
            [
                "--platform",
                "5",
                "--dry-run",
                "--tiktok-dir",
                str(project),
                "--tiktok-python",
                str(fake_python),
            ],
            output_fn=output.append,
            error_fn=output.append,
        )
        self.assertEqual(exit_code, 0, output)
        self.assertTrue(any("DRY-RUN" in item for item in output))

    def test_copied_tiktok_assets_are_portable_and_safe(self) -> None:
        collector = (REPO_ROOT / "tiktok" / "tiktok_collector.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("ROOT = Path(__file__).resolve().parent", collector)
        self.assertIn('default=str(ROOT / "keywords.txt")', collector)
        self.assertNotIn(r"E:\tiktok", collector)

        batch = REPO_ROOT / "tiktok" / "start_tiktok.bat"
        raw = batch.read_bytes()
        self.assertEqual(raw.decode("ascii").encode("ascii"), raw)
        text = raw.decode("ascii")
        self.assertIn('set "TIKTOK_DIR=%~dp0"', text)
        self.assertIn("find_adspower_port.ps1", text)
        self.assertNotIn(r"E:\tiktok", text)

        self.assertEqual(
            (REPO_ROOT / "tiktok" / "keywords.txt").read_text(encoding="utf-8"),
            "# One keyword or phrase per line.\n# Example: office chair\n",
        )
        self.assertEqual(
            set((REPO_ROOT / "tiktok" / "requirements.txt").read_text().split()),
            {"requests>=2.32.0", "websocket-client>=1.8.0", "openpyxl>=3.1.5"},
        )


if __name__ == "__main__":
    unittest.main()
