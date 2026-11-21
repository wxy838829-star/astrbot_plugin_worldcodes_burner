# -*- coding: utf-8 -*-
"""调用当前 LLM 的最小适配层。

只干三件事：

1. 从 AstrBot 的 Context 上取「当前生效」的 provider（不同版本方法名不一样，逐个探测）；
2. 用 provider.text_chat 提交一条短提示，并按对方签名决定是否传 max_tokens；
3. 把返回里的 usage 抠出来，抠不到就按字符数粗估。

不 import astrbot，方便脱离插件环境单测。
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any


class BurnError(Exception):
    """调用 LLM 失败，消息可直接回给用户。"""


@dataclass
class BurnResult:
    text: str
    total_tokens: int
    input_tokens: int
    output_tokens: int
    from_usage: bool


def estimate_tokens(text: str) -> int:
    """粗估 token：中日韩字符按 1 字 1 token，ASCII 按 4 字符 1 token。"""
    text = str(text or "")
    if not text:
        return 0
    wide = sum(1 for ch in text if ord(ch) >= 128)
    ascii_chars = len(text) - wide
    return max(1, wide + ascii_chars // 4)


def _accepted_kwargs(func: Any) -> set[str] | None:
    """函数签名允许的关键字参数；有 **kwargs 或拿不到签名时返回 None。"""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return None
    accepted: set[str] = set()
    for param in signature.parameters.values():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return None
        if param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            accepted.add(param.name)
    return accepted


def _read_attr(obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(obj, dict):
            value = obj.get(name)
        else:
            value = getattr(obj, name, None)
        if value not in (None, ""):
            return value
    return None


def extract_usage(resp: Any) -> tuple[int, int, int, bool]:
    """返回 (total, input, output, 是否来自服务端 usage)。"""
    usage = _read_attr(resp, "usage", "token_usage")
    if usage is not None:
        total = _read_attr(usage, "total_tokens", "total")
        prompt = _read_attr(usage, "input_tokens", "prompt_tokens")
        completion = _read_attr(usage, "output_tokens", "completion_tokens")

        def to_int(value: Any) -> int:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return 0

        total_i = to_int(total)
        input_i = to_int(prompt)
        output_i = to_int(completion)
        if not total_i:
            total_i = input_i + output_i
        if total_i:
            return total_i, input_i, output_i, True
    return 0, 0, 0, False


def extract_text(resp: Any) -> str:
    if isinstance(resp, str):
        return resp
    text = _read_attr(resp, "completion_text", "completion", "text", "content")
    if text is None:
        return ""
    return str(text)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def resolve_provider(context: Any, provider_id: str = "") -> Any | None:
    """按版本差异探测 provider 获取方法，优先指定 ID，其次当前会话默认。"""
    provider_id = str(provider_id or "").strip()

    if provider_id:
        for name in ("get_provider_by_id", "get_provider"):
            getter = getattr(context, name, None)
            if not callable(getter):
                continue
            try:
                provider = await _maybe_await(getter(provider_id))
            except Exception:
                continue
            if provider is not None:
                return provider

    for name in ("get_using_provider", "get_provider"):
        getter = getattr(context, name, None)
        if not callable(getter):
            continue
        for call in (lambda: getter(), lambda: getter(None)):
            try:
                provider = await _maybe_await(call())
            except TypeError:
                continue
            except Exception:
                continue
            if provider is not None:
                return provider
    return None


async def burn_once(
    provider: Any,
    *,
    prompt: str,
    system_prompt: str = "",
    max_tokens: int = 16,
    session_id: str = "worldcodes_burner",
    timeout: float = 60.0,
) -> BurnResult:
    """提交一条无业务意义的短提示，返回消耗情况。"""
    chat = getattr(provider, "text_chat", None)
    if not callable(chat):
        raise BurnError("当前 LLM 提供方不支持 text_chat，没法空转")

    kwargs: dict[str, Any] = {
        "prompt": prompt,
        "contexts": [],
        "image_urls": [],
        "session_id": session_id,
    }
    if system_prompt:
        kwargs["system_prompt"] = system_prompt

    accepted = _accepted_kwargs(chat)
    if accepted is None:
        kwargs["max_tokens"] = max_tokens
        strict = False
    else:
        if "max_tokens" in accepted:
            kwargs["max_tokens"] = max_tokens
        strict = True
        kwargs = {k: v for k, v in kwargs.items() if k in accepted}

    try:
        resp = await asyncio.wait_for(chat(**kwargs), timeout=timeout)
    except TypeError as exc:
        # 只有「不认识这个关键字参数」才退回最小参数重试，别的 TypeError 不能重发，
        # 否则可能把一次已经发出去的请求变成两次，白花钱。
        if strict or "unexpected keyword" not in str(exc):
            raise BurnError(f"LLM 调用失败：{exc}") from exc
        try:
            resp = await asyncio.wait_for(
                chat(prompt=prompt, session_id=session_id), timeout=timeout
            )
        except asyncio.TimeoutError as retry_exc:
            raise BurnError(f"LLM 调用超时（>{timeout:g}s）") from retry_exc
        except Exception as retry_exc:
            raise BurnError(f"LLM 调用失败：{retry_exc}") from retry_exc
    except asyncio.TimeoutError as exc:
        raise BurnError(f"LLM 调用超时（>{timeout:g}s）") from exc
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise BurnError(f"LLM 调用失败：{exc}") from exc

    text = extract_text(resp)
    total, input_tokens, output_tokens, from_usage = extract_usage(resp)
    if not from_usage:
        input_tokens = estimate_tokens(prompt) + estimate_tokens(system_prompt)
        output_tokens = estimate_tokens(text)
        total = input_tokens + output_tokens
    return BurnResult(
        text=text,
        total_tokens=max(1, total),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        from_usage=from_usage,
    )
