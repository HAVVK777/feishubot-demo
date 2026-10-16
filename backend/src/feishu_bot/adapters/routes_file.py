"""从 JSON 文件读路由表。

配置文件是**你的**，不是代码的：加一个外部服务只要改这个文件，不用碰 Python。
代价是写错的时候编译器帮不上忙，所以这里的校验要尽量狠——**配置写错要在启动时就
报出来**，而不是等某条消息进来才发现。

这个模块只负责"把文件读成一串合法的 `RouteSpec`"，不认识 `Rule` 也不认识 HTTP
客户端。把两者接起来是 `bootstrap.py` 的事。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 10.0

#: 配置里允许出现的字段。多出来的字段一律报错——`endpont` 这种拼写错误
#: 如果被静默忽略，表现是"这个服务永远调不通"，排查起来很费时间。
KNOWN_FIELDS = frozenset(
    {"keyword", "endpoint", "description", "aliases", "headers", "timeoutSeconds"}
)


class RouteConfigError(RuntimeError):
    """配置有问题。故意用 RuntimeError 的子类：它应该在启动时冒到最上面。"""


@dataclass(frozen=True, slots=True)
class RouteSpec:
    """配置文件里的一条。还不是一条 `Rule`，也还不是一个服务。"""

    keyword: str
    endpoint: str
    description: str = ""
    #: 额外的正文触发词。留空表示只能通过 `/关键字 内容` 触发。
    aliases: tuple[str, ...] = ()
    #: 调用这个服务时附带的请求头，用来放鉴权信息。
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


def load_routes(path: Path) -> tuple[RouteSpec, ...]:
    """读配置文件。任何问题都抛 `RouteConfigError` 并指明是哪一条。"""
    document = _read_document(path)

    entries = document.get("routes")
    if not isinstance(entries, list):
        raise RouteConfigError(f'{path} 里缺少 "routes" 数组')

    return tuple(_parse_entry(entry, index, path) for index, entry in enumerate(entries))


def _read_document(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RouteConfigError(f"找不到路由配置文件：{path}") from exc
    except OSError as exc:
        raise RouteConfigError(f"读不了路由配置文件 {path}：{exc}") from exc

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RouteConfigError(f"{path} 不是合法 JSON：{exc}") from exc

    if not isinstance(document, dict):
        raise RouteConfigError(f"{path} 的顶层应该是一个对象")

    unknown = sorted(set(document) - {"routes"})
    if unknown:
        raise RouteConfigError(f"{path} 顶层有无法识别的字段：{'、'.join(unknown)}")

    return document


def _parse_entry(entry: Any, index: int, path: Path) -> RouteSpec:
    if not isinstance(entry, dict):
        raise RouteConfigError(f"{path} 的第 {index + 1} 条 route 应该是一个对象")

    keyword = _require_text(entry, "keyword", index, path)
    where = f'{path} 的第 {index + 1} 条 route（"{keyword}"）'

    unknown = sorted(set(entry) - KNOWN_FIELDS)
    if unknown:
        raise RouteConfigError(f"{where}有无法识别的字段：{'、'.join(unknown)}")

    endpoint = _require_text(entry, "endpoint", index, path)
    if not endpoint.startswith(("http://", "https://")):
        raise RouteConfigError(
            f'{where}的 "endpoint" 要以 http:// 或 https:// 开头，得到 {endpoint!r}'
        )

    return RouteSpec(
        keyword=keyword,
        endpoint=endpoint,
        description=_optional_text(entry, "description", where),
        aliases=_optional_text_tuple(entry, "aliases", where),
        headers=_optional_text_map(entry, "headers", where),
        timeout_seconds=_optional_positive_number(entry, "timeoutSeconds", where),
    )


def _require_text(entry: Mapping[str, Any], field_name: str, index: int, path: Path) -> str:
    value = entry.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise RouteConfigError(f'{path} 的第 {index + 1} 条 route 缺少非空的 "{field_name}"')
    return value.strip()


def _optional_text(entry: Mapping[str, Any], field_name: str, where: str) -> str:
    value = entry.get(field_name, "")
    if not isinstance(value, str):
        raise RouteConfigError(f'{where}的 "{field_name}" 应该是字符串')
    return value


def _optional_text_tuple(entry: Mapping[str, Any], field_name: str, where: str) -> tuple[str, ...]:
    value = entry.get(field_name, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RouteConfigError(f'{where}的 "{field_name}" 应该是字符串数组')
    return tuple(value)


def _optional_text_map(entry: Mapping[str, Any], field_name: str, where: str) -> Mapping[str, str]:
    value = entry.get(field_name, {})
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        raise RouteConfigError(f'{where}的 "{field_name}" 应该是字符串到字符串的对象')

    for key, item in value.items():
        _require_ascii(key, field_name, where)
        _require_ascii(item, field_name, where)

    return value


def _require_ascii(value: str, field_name: str, where: str) -> None:
    """HTTP 头只能是 ASCII。

    不在这里拦的话，错误要等到第一次真正外呼才炸，而且抛的是 `UnicodeEncodeError`
    ——看起来像代码 bug 而不是配置问题。在 token 旁边写一句中文备注是最容易踩的一种。
    """
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise RouteConfigError(
            f'{where}的 "{field_name}" 里有非 ASCII 字符（{value!r}）——HTTP 头只能是 ASCII'
        ) from exc


def _optional_positive_number(entry: Mapping[str, Any], field_name: str, where: str) -> float:
    value = entry.get(field_name, DEFAULT_TIMEOUT_SECONDS)
    # bool 是 int 的子类，`true` 会被当成 1 通过——显式排掉。
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise RouteConfigError(f'{where}的 "{field_name}" 应该是正数')
    return float(value)
