"""hints 要真的出现在模型看得到的描述里。"""

from __future__ import annotations

import pytest

from feishubot_agent.harness.model import ToolSpec
from feishubot_agent.harness.tools import ToolCatalog, with_hints

SEARCH = ToolSpec(name="search", description="检索知识库", input_schema={})


def test_no_hints_leaves_description_alone() -> None:
    assert with_hints(SEARCH) is SEARCH


def test_read_only_hint() -> None:
    assert with_hints(SEARCH, read_only=True).description == "检索知识库（只读）"


def test_idempotent_hint_implies_side_effects() -> None:
    description = with_hints(SEARCH, idempotent=True).description
    assert description == "检索知识库（有副作用，可重复调用）"


def test_destructive_hint() -> None:
    description = with_hints(SEARCH, destructive=True).description
    assert description == "检索知识库（有副作用，破坏性）"


def test_catalog_filters_by_allowlist() -> None:
    other = ToolSpec(name="delete", description="删除", input_schema={})
    catalog = ToolCatalog([SEARCH, other], allowed=("search",))

    assert [spec.name for spec in catalog.specs] == ["search"]


def test_catalog_rejects_duplicate_names() -> None:
    with pytest.raises(ValueError, match="重名"):
        ToolCatalog([SEARCH, SEARCH])


def test_catalog_rejects_unknown_allowlist_entries() -> None:
    with pytest.raises(ValueError, match="不存在"):
        ToolCatalog([SEARCH], allowed=("missing",))
