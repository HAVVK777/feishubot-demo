"""`.env` 加载器：解析规则、优先级、错误。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from feishubot_mcp import env_file


@pytest.fixture
def env_guard():
    """load() 直接改 os.environ，测试后整体还原。"""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_parses_simple_pairs(env_guard, tmp_path: Path):
    env_file.load(write(tmp_path, "TEST_A=1\nTEST_B=hello\n"))

    assert os.environ["TEST_A"] == "1"
    assert os.environ["TEST_B"] == "hello"


def test_does_not_override_existing_variables(env_guard, tmp_path: Path, monkeypatch):
    """容器/进程管理器注入的值优先——.env 只是兜底。"""
    monkeypatch.setenv("TEST_A", "from-env")

    env_file.load(write(tmp_path, "TEST_A=from-file\n"))

    assert os.environ["TEST_A"] == "from-env"


def test_supports_export_quotes_and_comments(env_guard, tmp_path: Path):
    env_file.load(
        write(
            tmp_path,
            "\n".join(
                [
                    "# 注释",
                    "export TEST_A=1",
                    'TEST_B="two words"',
                    "TEST_C='quoted'",
                    "TEST_D=value # trailing comment",
                    "TEST_E=hash#inside",  # # 前面没空白，不是注释
                ]
            ),
        )
    )

    assert os.environ["TEST_A"] == "1"
    assert os.environ["TEST_B"] == "two words"
    assert os.environ["TEST_C"] == "quoted"
    assert os.environ["TEST_D"] == "value"
    assert os.environ["TEST_E"] == "hash#inside"


def test_missing_file_is_not_an_error(env_guard, tmp_path: Path):
    assert env_file.load(tmp_path / "nope.env") == 0


def test_malformed_line_fails_loudly(env_guard, tmp_path: Path):
    with pytest.raises(RuntimeError, match="第 2 行"):
        env_file.load(write(tmp_path, "TEST_A=1\nnot-a-pair\n"))


def test_uses_MCP_ENV_FILE(env_guard, tmp_path: Path, monkeypatch):
    path = write(tmp_path, "TEST_A=from-custom\n")
    monkeypatch.setenv(env_file.ENV_FILE_ENV, str(path))

    assert env_file.load() == 1
    assert os.environ["TEST_A"] == "from-custom"


def test_empty_file_loads_nothing(env_guard, tmp_path: Path):
    assert env_file.load(write(tmp_path, "\n# 只有注释\n")) == 0
