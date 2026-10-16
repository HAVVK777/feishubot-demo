"""配置文件读取的测试。

配置写错是**必然**会发生的（它是手写的 JSON，编译器帮不上忙），所以这里的重点不是
"能读对"，而是"**读错的时候报错指不指得清是哪一条**"。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from feishu_bot.adapters.routes_file import DEFAULT_TIMEOUT_SECONDS, RouteConfigError, load_routes


def write(tmp_path: Path, document: Any) -> Path:
    path = tmp_path / "routes.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def one_route(**overrides: Any) -> dict[str, Any]:
    entry = {"keyword": "搜索", "endpoint": "https://service.test/search"}
    entry.update(overrides)
    return {"routes": [entry]}


class TestHappyPath:
    def test_reads_a_route(self, tmp_path: Path) -> None:
        path = write(tmp_path, one_route(description="检索", aliases=["查一下"]))

        (route,) = load_routes(path)

        assert route.keyword == "搜索"
        assert route.endpoint == "https://service.test/search"
        assert route.description == "检索"
        assert route.aliases == ("查一下",)

    def test_optional_fields_have_defaults(self, tmp_path: Path) -> None:
        path = write(tmp_path, one_route())

        (route,) = load_routes(path)

        assert route.description == ""
        assert route.aliases == ()
        assert route.headers == {}
        assert route.timeout_seconds == DEFAULT_TIMEOUT_SECONDS

    def test_headers_are_read(self, tmp_path: Path) -> None:
        path = write(tmp_path, one_route(headers={"Authorization": "Bearer t"}))

        (route,) = load_routes(path)

        assert route.headers == {"Authorization": "Bearer t"}

    def test_timeout_is_read(self, tmp_path: Path) -> None:
        path = write(tmp_path, one_route(timeoutSeconds=30))

        (route,) = load_routes(path)

        assert route.timeout_seconds == 30.0

    def test_empty_route_list_is_allowed(self, tmp_path: Path) -> None:
        """一条都没有是合法的——机器人只会回 /帮助，bootstrap 会打一条 warning。"""
        assert load_routes(write(tmp_path, {"routes": []})) == ()

    def test_keyword_is_trimmed(self, tmp_path: Path) -> None:
        path = write(tmp_path, one_route(keyword="  搜索  "))

        assert load_routes(path)[0].keyword == "搜索"


class TestFileProblems:
    def test_missing_file_names_the_path(self, tmp_path: Path) -> None:
        with pytest.raises(RouteConfigError, match="找不到"):
            load_routes(tmp_path / "nope.json")

    def test_broken_json_is_reported(self, tmp_path: Path) -> None:
        path = tmp_path / "routes.json"
        path.write_text("{ 不是 JSON", encoding="utf-8")

        with pytest.raises(RouteConfigError, match="不是合法 JSON"):
            load_routes(path)

    def test_top_level_must_be_an_object(self, tmp_path: Path) -> None:
        with pytest.raises(RouteConfigError, match="顶层"):
            load_routes(write(tmp_path, []))

    def test_missing_routes_key(self, tmp_path: Path) -> None:
        with pytest.raises(RouteConfigError, match="routes"):
            load_routes(write(tmp_path, {}))

    def test_unknown_top_level_field(self, tmp_path: Path) -> None:
        with pytest.raises(RouteConfigError, match="routez"):
            load_routes(write(tmp_path, {"routes": [], "routez": []}))


class TestEntryProblems:
    """每一条报错都要能定位到是第几条。"""

    def test_entry_must_be_an_object(self, tmp_path: Path) -> None:
        with pytest.raises(RouteConfigError, match="第 1 条"):
            load_routes(write(tmp_path, {"routes": ["搜索"]}))

    def test_missing_keyword(self, tmp_path: Path) -> None:
        with pytest.raises(RouteConfigError, match="keyword"):
            load_routes(write(tmp_path, {"routes": [{"endpoint": "https://x.test"}]}))

    def test_blank_keyword(self, tmp_path: Path) -> None:
        with pytest.raises(RouteConfigError, match="keyword"):
            load_routes(write(tmp_path, one_route(keyword="   ")))

    def test_missing_endpoint(self, tmp_path: Path) -> None:
        with pytest.raises(RouteConfigError, match="endpoint"):
            load_routes(write(tmp_path, {"routes": [{"keyword": "搜索"}]}))

    def test_endpoint_needs_a_scheme(self, tmp_path: Path) -> None:
        with pytest.raises(RouteConfigError, match="http"):
            load_routes(write(tmp_path, one_route(endpoint="127.0.0.1:9001/search")))

    def test_error_identifies_the_offending_route(self, tmp_path: Path) -> None:
        """两条路由时，报错要指明是第二条——不然还得自己数。"""
        document = {
            "routes": [
                {"keyword": "搜索", "endpoint": "https://service.test/search"},
                {"keyword": "摘要", "endpoint": "没写协议"},
            ]
        }

        with pytest.raises(RouteConfigError, match="第 2 条"):
            load_routes(write(tmp_path, document))

    @pytest.mark.parametrize("typo", ["endpont", "endpoit", "url"])
    def test_typo_in_a_field_name_is_caught(self, tmp_path: Path, typo: str) -> None:
        """拼错字段名最隐蔽：静默忽略的话表现是"这个服务永远调不通"。"""
        entry = {"keyword": "搜索", typo: "https://service.test/search"}

        with pytest.raises(RouteConfigError):
            load_routes(write(tmp_path, {"routes": [entry]}))

    def test_aliases_must_be_a_string_list(self, tmp_path: Path) -> None:
        with pytest.raises(RouteConfigError, match="aliases"):
            load_routes(write(tmp_path, one_route(aliases="查一下")))

    def test_headers_must_be_a_string_map(self, tmp_path: Path) -> None:
        with pytest.raises(RouteConfigError, match="headers"):
            load_routes(write(tmp_path, one_route(headers={"X": 1})))

    def test_header_values_must_be_ascii(self, tmp_path: Path) -> None:
        """HTTP 头只能是 ASCII。

        不在启动时拦的话，第一次外呼会抛 `UnicodeEncodeError`——看起来像代码 bug
        而不是配置问题。在 token 旁边写一句中文备注是最容易踩的一种。
        """
        headers = {"Authorization": "Bearer 你的 token"}

        with pytest.raises(RouteConfigError, match="ASCII"):
            load_routes(write(tmp_path, one_route(headers=headers)))

    def test_header_names_must_be_ascii(self, tmp_path: Path) -> None:
        with pytest.raises(RouteConfigError, match="ASCII"):
            load_routes(write(tmp_path, one_route(headers={"授权": "x"})))

    def test_description_must_be_a_string(self, tmp_path: Path) -> None:
        with pytest.raises(RouteConfigError, match="description"):
            load_routes(write(tmp_path, one_route(description=[])))

    @pytest.mark.parametrize("value", [0, -1, "30", None, True])
    def test_timeout_must_be_a_positive_number(self, tmp_path: Path, value: Any) -> None:
        with pytest.raises(RouteConfigError, match="timeoutSeconds"):
            load_routes(write(tmp_path, one_route(timeoutSeconds=value)))
