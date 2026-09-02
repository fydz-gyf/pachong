#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""三个电商平台爬虫的统一启动器。

这个模块只负责选择平台、做启动前检查并启动已有入口，不复制任何平台的
抓取逻辑。平台自己的交互式参数输入和输出目录仍由各自项目负责。

示例::

    python unified_scraper.py
    python unified_scraper.py --platform taobao
    python unified_scraper.py --platform walmart --dry-run
    python unified_scraper.py --platform wayfair
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


PLATFORMS = ("taobao", "walmart", "wayfair")
PLATFORM_LABELS = {
    "taobao": "淘宝",
    "walmart": "Walmart",
    "wayfair": "Wayfair",
}
PLATFORM_ALIASES = {
    "1": "taobao",
    "淘宝": "taobao",
    "taobao": "taobao",
    "tb": "taobao",
    "2": "walmart",
    "沃尔玛": "walmart",
    "walmart": "walmart",
    "wm": "walmart",
    "3": "wayfair",
    "wayfair": "wayfair",
    "退出": None,
    "exit": None,
    "quit": None,
    "q": None,
    "0": None,
}


class LauncherError(RuntimeError):
    """统一启动器可以向用户解释的预检或启动错误。"""


@dataclass(frozen=True)
class ProjectPaths:
    """三个项目的根目录。目录可通过命令行或环境变量覆盖。"""

    taobao: Path
    walmart: Path
    wayfair: Path


@dataclass(frozen=True)
class LaunchPlan:
    """一个平台的完整子进程启动计划，便于测试而不实际启动爬虫。"""

    platform: str
    label: str
    cwd: Path
    entrypoint: Path
    command: tuple[str, ...]
    runtime_executable: Path | None = None


def normalize_platform(value: str) -> str | None:
    """将菜单编号、中文名称或英文名称转换为平台 key。

    返回 ``None`` 表示用户选择退出；未知输入抛出 ``ValueError``，交给
    交互菜单继续询问或交给 argparse 输出命令行错误。
    """

    key = str(value or "").strip().lower()
    if key not in PLATFORM_ALIASES:
        raise ValueError(
            f"不支持的平台：{value!r}。请选择 taobao、walmart、wayfair 或 0。"
        )
    return PLATFORM_ALIASES[key]


def choose_platform(
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], object] = print,
) -> str | None:
    """显示中文菜单并返回平台 key；返回 ``None`` 表示退出。"""

    output_fn("\n" + "=" * 64)
    output_fn("统一电商平台爬虫启动器")
    output_fn("请选择要运行的平台：")
    output_fn("  1. 淘宝")
    output_fn("  2. Walmart")
    output_fn("  3. Wayfair")
    output_fn("  0. 退出")
    output_fn("=" * 64)

    while True:
        try:
            raw = input_fn("请输入选项 [1-3/0]: ")
        except EOFError as exc:
            raise LauncherError("无法读取平台选择，请在可交互的命令窗口中重新运行。") from exc
        except KeyboardInterrupt as exc:
            raise LauncherError("平台选择已取消。") from exc

        try:
            return normalize_platform(raw)
        except ValueError as exc:
            output_fn(f"[错误] {exc}")


def _path_value(
    value: str | Path | None,
    env_name: str,
    default: Path,
    environ: Mapping[str, str],
) -> Path:
    raw = value if value is not None else environ.get(env_name)
    if raw is None or not str(raw).strip():
        raw = default
    # resolve(strict=False) keeps error messages absolute even when the path
    # does not exist yet, without creating directories or touching project data.
    return Path(raw).expanduser().resolve()


def load_project_paths(
    *,
    taobao_dir: str | Path | None = None,
    walmart_dir: str | Path | None = None,
    wayfair_dir: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    launcher_dir: Path | None = None,
) -> ProjectPaths:
    """读取默认目录及可选覆盖项。

    环境变量名称分别为 ``TAOBAO_SCRAPER_DIR``、
    ``WALMART_SCRAPER_DIR`` 和 ``WAYFAIR_SCRAPER_DIR``。
    """

    env = os.environ if environ is None else environ
    root = (launcher_dir or Path(__file__).resolve().parent).resolve()
    return ProjectPaths(
        taobao=_path_value(taobao_dir, "TAOBAO_SCRAPER_DIR", root / "taobao", env),
        walmart=_path_value(
            walmart_dir, "WALMART_SCRAPER_DIR", root / "walmart", env
        ),
        wayfair=_path_value(
            wayfair_dir,
            "WAYFAIR_SCRAPER_DIR",
            root / "wayfair",
            env,
        ),
    )


def _command_interpreter(value: str | Path | None = None) -> Path:
    """返回 Windows cmd.exe；仅 Wayfair 的既有 BAT 入口需要它。"""

    if value is not None:
        return Path(value)
    configured = os.environ.get("COMSPEC") or os.environ.get("ComSpec")
    if configured:
        return Path(configured)
    found = shutil.which("cmd.exe")
    return Path(found) if found else Path("cmd.exe")


def build_launch_plan(
    platform: str,
    paths: ProjectPaths | None = None,
    *,
    python_executable: str | Path | None = None,
    command_interpreter: str | Path | None = None,
) -> LaunchPlan:
    """构造一个平台的命令和工作目录，不执行命令。"""

    normalized = normalize_platform(platform)
    if normalized is None:
        raise ValueError("退出选项不能生成启动计划。")
    project_paths = paths or load_project_paths()

    if normalized == "taobao":
        project_dir = project_paths.taobao
        entrypoint = project_dir / "taobao_scraper.py"
        runtime = Path(python_executable or sys.executable)
        command = (str(runtime), str(entrypoint), "--interactive")
    elif normalized == "walmart":
        project_dir = project_paths.walmart
        entrypoint = project_dir / "run.py"
        runtime = project_dir / ".venv" / "Scripts" / "python.exe"
        # 与 Walmart 现有 BAT 保持一致，使用 AdsPower 浏览器身份并保留其
        # run.py 内置的交互式关键词/页数输入。
        command = (str(runtime), str(entrypoint), "--browser", "adspower")
    else:
        project_dir = project_paths.wayfair
        entrypoint = project_dir / "启动Wayfair类目采集.bat"
        runtime = None
        interpreter = _command_interpreter(command_interpreter)
        # BAT 中包含产品列表、评论和图片嵌入的完整流程，因此直接委托给
        # 该既有入口；cmd.exe 只是 Windows 启动 .bat 所需的显式解释器。
        command = (
            str(interpreter),
            "/d",
            "/c",
            f'call "{entrypoint}"',
        )

    return LaunchPlan(
        platform=normalized,
        label=PLATFORM_LABELS[normalized],
        cwd=project_dir,
        entrypoint=entrypoint,
        command=command,
        runtime_executable=runtime,
    )


def _available_executable(value: str | Path) -> bool:
    candidate = Path(value)
    if candidate.is_file():
        return True
    return shutil.which(str(value)) is not None


def preflight(plan: LaunchPlan) -> None:
    """检查项目目录、入口文件和运行时；失败时给出中文诊断。"""

    problems: list[str] = []
    if not plan.cwd.is_dir():
        problems.append(f"未找到项目目录：{plan.cwd}")
    if not plan.entrypoint.is_file():
        problems.append(f"未找到启动入口：{plan.entrypoint}")
    if plan.runtime_executable is not None and not _available_executable(
        plan.runtime_executable
    ):
        problems.append(f"未找到 Python 运行时：{plan.runtime_executable}")
    if plan.platform == "wayfair" and not _available_executable(plan.command[0]):
        problems.append(
            f"未找到 Windows 命令解释器：{plan.command[0]}（需要 cmd.exe 启动 Wayfair BAT）"
        )

    if problems:
        details = "\n".join(f"  - {problem}" for problem in problems)
        raise LauncherError(
            f"{plan.label} 启动前检查失败，请检查路径和 Python 环境：\n{details}"
        )


def format_command(command: Sequence[str]) -> str:
    """按 Windows 命令行规则格式化命令，仅用于显示，不用于执行。"""

    return subprocess.list2cmdline([str(part) for part in command])


def execute_plan(
    plan: LaunchPlan,
    *,
    runner: Callable[..., object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """执行启动计划并原样返回子进程退出码。

    不使用 ``shell=True``；Wayfair 的 BAT 通过命令列表中的显式 cmd.exe
    启动。子进程继承标准输入/输出，所以三个原有交互流程不变。
    """

    run = runner or subprocess.run
    child_env = dict(os.environ if environ is None else environ)
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    try:
        result = run(
            list(plan.command),
            cwd=str(plan.cwd),
            env=child_env,
            shell=False,
            check=False,
        )
    except OSError as exc:
        raise LauncherError(f"启动 {plan.label} 失败：{exc}") from exc
    return int(getattr(result, "returncode", 1))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unified_scraper",
        description="选择并启动淘宝、Walmart 或 Wayfair 的现有爬虫入口。",
    )
    parser.add_argument(
        "--platform",
        type=_platform_arg,
        metavar="{taobao,walmart,wayfair}",
        help="直接选择平台；省略时显示交互式菜单。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只显示启动命令并完成预检，不启动爬虫。",
    )
    parser.add_argument("--taobao-dir", help="淘宝项目目录（默认：启动器所在目录）。")
    parser.add_argument(
        "--walmart-dir",
        help="Walmart 项目目录（默认：仓库根目录/walmart）。",
    )
    parser.add_argument(
        "--wayfair-dir",
        help="Wayfair 项目目录（默认：仓库根目录/wayfair）。",
    )
    parser.add_argument(
        "--taobao-python",
        help="淘宝使用的 Python 可执行文件（默认：当前启动器 Python）。",
    )
    parser.add_argument(
        "--cmd-exe",
        help="Wayfair BAT 使用的 cmd.exe 路径（默认读取 COMSPEC）。",
    )
    return parser


def _platform_arg(value: str) -> str:
    try:
        normalized = normalize_platform(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if normalized is None:
        raise argparse.ArgumentTypeError("--platform 不能选择退出，请省略参数后在菜单中选择。")
    return normalized


def main(
    argv: Sequence[str] | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], object] = print,
    error_fn: Callable[[str], object] = print,
) -> int:
    """命令行入口；返回值可直接作为 BAT 的 ERRORLEVEL。"""

    args = build_parser().parse_args(argv)
    platform = args.platform
    if platform is None:
        try:
            platform = choose_platform(input_fn=input_fn, output_fn=output_fn)
        except LauncherError as exc:
            error_fn(f"[错误] {exc}")
            return 2
        if platform is None:
            output_fn("已退出，未启动任何爬虫。")
            return 0

    paths = load_project_paths(
        taobao_dir=args.taobao_dir,
        walmart_dir=args.walmart_dir,
        wayfair_dir=args.wayfair_dir,
    )
    plan = build_launch_plan(
        platform,
        paths,
        python_executable=args.taobao_python,
        command_interpreter=args.cmd_exe,
    )

    try:
        preflight(plan)
        output_fn(f"平台：{plan.label}")
        output_fn(f"工作目录：{plan.cwd}")
        output_fn(f"启动命令：{format_command(plan.command)}")
        if args.dry_run:
            output_fn("[DRY-RUN] 预检通过，未启动爬虫。")
            return 0
        output_fn("正在启动，后续参数输入由该平台原有程序负责。\n")
        return execute_plan(plan)
    except LauncherError as exc:
        error_fn(f"[错误] {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
