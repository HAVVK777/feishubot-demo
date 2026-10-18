"""把分层规则钉在测试里。

目录结构本身不会阻止 `harness` 顺手 import 一个业务模块；这些测试会。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_NAME = "feishubot_agent"
PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / PACKAGE_NAME
HARNESS_ROOT = PACKAGE_ROOT / "harness"


def _python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    return ".".join((PACKAGE_NAME, *relative.parts))


def _resolve_relative(path: Path, module: str | None, level: int) -> str:
    """把 `from ..app import x` 还原成完整模块名，相对导入不能成为绕过边界的后门。"""
    package_parts = _module_name(path).split(".")[:-1]
    base = package_parts[: len(package_parts) - (level - 1)]
    if module:
        base.extend(module.split("."))
    return ".".join(base)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _resolve_relative(path, node.module, node.level)
            else:
                base = node.module or ""
            if base:
                modules.add(base)
            # `from feishubot_agent import app` / `from .. import app` 也要能识别。
            modules.update(f"{base}.{alias.name}" for alias in node.names if base)
    return modules


def _near(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


@pytest.mark.parametrize("path", _python_files(HARNESS_ROOT), ids=lambda p: _near(p, HARNESS_ROOT))
def test_harness_does_not_depend_on_app_or_entrypoints(path: Path) -> None:
    forbidden = sorted(
        module
        for module in _imports(path)
        if module == "feishubot_agent.app"
        or module.startswith("feishubot_agent.app.")
        or module in {"feishubot_agent.server", "feishubot_agent.bootstrap"}
    )
    assert not forbidden, f"{_near(path, HARNESS_ROOT)} 不能 import {forbidden}"


@pytest.mark.parametrize("path", _python_files(HARNESS_ROOT), ids=lambda p: _near(p, HARNESS_ROOT))
def test_model_sdk_only_in_the_provider(path: Path) -> None:
    if path == HARNESS_ROOT / "providers" / "deepseek.py":
        return
    offenders = sorted(
        module for module in _imports(path) if module == "openai" or module.startswith("openai.")
    )
    assert not offenders, f"{_near(path, HARNESS_ROOT)} 只能通过 ModelClient 协议使用模型"


@pytest.mark.parametrize("path", _python_files(HARNESS_ROOT), ids=lambda p: _near(p, HARNESS_ROOT))
def test_mcp_only_in_the_client(path: Path) -> None:
    if path == HARNESS_ROOT / "mcp_client.py":
        return
    offenders = sorted(
        module for module in _imports(path) if module == "mcp" or module.startswith("mcp.")
    )
    assert not offenders, f"{_near(path, HARNESS_ROOT)} 只能通过 ToolExecutor 协议调用工具"


@pytest.mark.parametrize(
    "path", _python_files(PACKAGE_ROOT / "app"), ids=lambda p: _near(p, PACKAGE_ROOT)
)
def test_app_uses_harness_not_transport_details(path: Path) -> None:
    offenders = sorted(
        module
        for module in _imports(path)
        if module == "openai"
        or module.startswith("openai.")
        or module == "mcp"
        or module.startswith("mcp.")
    )
    assert not offenders, f"{_near(path, PACKAGE_ROOT)} 不能直接 import 模型 SDK 或 MCP"
