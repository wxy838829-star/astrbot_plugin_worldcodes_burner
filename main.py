# -*- coding: utf-8 -*-
"""astrbot_plugin_worldcodes_burner

一个「空转」插件：按配置的节奏，向当前生效的 LLM 反复提交没有任何业务意义的短提示，
把 token 消耗掉。默认关闭，必须明确启用。

为什么闸门这么多：

- 空转是纯消耗，脚本一失控就是持续扣费，所以默认必须是关的；
- 所有配置在运行时都会 clamp 到硬上限，配置文件里写 1 秒 / 100 万 tokens 无效；
- 每日预算落盘，插件重载、进程重启都不清零，不能靠重启绕过限额；
- 单次运行有最长时限，到点自动停，防止开完忘了关；
- 连续 3 次调不通自动停，避免对着坏掉的 provider 空转；
- /烧token 停 随时可用，直接取消循环任务。

指令：/烧token [状态|开|停|帮助]，另有 /停烧 快捷命令。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.message_event_result import MessageChain

from .budget import (
    HARD_DAILY_BUDGET_TOKENS,
    HARD_DAILY_REQUESTS,
    HARD_MAX_RUNTIME_MINUTES,
    HARD_MAX_TOKENS_PER_REQUEST,
    MAX_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    BudgetLedger,
    BurnLimits,
    clamp_int,
    clamp_prompt,
)
from .llm import burn_once, resolve_provider

PLUGIN_NAME = "astrbot_plugin_worldcodes_burner"

# 连续失败多少次就自动停
MAX_CONSECUTIVE_FAILURES = 3

_COMMAND_NAMES = ("烧token", "burn", "空转")


@dataclass
class _Runtime:
    """一轮运行的现场状态，停止时整体丢弃。"""

    started_at: float = 0.0
    ok: int = 0
    fail: int = 0
    last_error: str = ""
    last_text: str = ""


class TokenBurnerPlugin(Star):
    """定时提交短提示、空转消耗 token 的插件。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self.data_dir: Path = StarTools.get_data_dir(PLUGIN_NAME)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = BudgetLedger(self.data_dir / "budget.json")
        self._loop_task: asyncio.Task | None = None
        self._running = False
        self._session = ""
        self._overrides: dict[str, int] = {}
        self._runtime = _Runtime()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """只有配置里 enabled=true 才自动开跑；默认什么都不做。"""
        self.ledger.rollover()
        if not bool(self._cfg("enabled", False)):
            logger.info(f"[{PLUGIN_NAME}] 默认关闭，等 /烧token 开")
            return
        logger.info(f"[{PLUGIN_NAME}] 配置已启用，开始空转")
        self._start(str(self._cfg("notify_session", "")))

    async def terminate(self) -> None:
        self._cancel_loop()
        self.ledger.save()

    # ------------------------------------------------------------------
    # 配置与工具
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)  # type: ignore[union-attr]
        except Exception:
            value = default
        return default if value in (None, "") else value

    def _limits(self) -> BurnLimits:
        return BurnLimits.from_config(
            self.config,
            interval=self._overrides.get("interval"),
            max_tokens=self._overrides.get("max_tokens"),
        )

    def _prompt(self) -> str:
        return clamp_prompt(str(self._cfg("prompt", "嗯")))

    def _ready(self, event: AstrMessageEvent) -> str | None:
        """返回拦截提示；None 表示放行。空转会花钱，默认只给管理员。"""
        if str(self._cfg("permission", "仅管理员")) != "仅管理员":
            return None
        allow_ids = {
            str(x).strip()
            for x in (self._cfg("extra_user_ids", []) or [])
            if str(x).strip()
        }
        try:
            is_admin = bool(event.is_admin())
        except Exception:
            is_admin = False
        try:
            sender = event.get_sender_id()
        except Exception:
            sender = ""
        if not is_admin and sender not in allow_ids:
            return "空转会真花钱，先让管理员来。"
        return None

    @staticmethod
    def _strip_command(text: str, *names: str) -> str:
        raw = (text or "").strip()
        raw = raw.lstrip("/／!！").strip()
        for name in names:
            if raw.lower().startswith(name.lower()):
                return raw[len(name) :].strip()
        return raw

    @staticmethod
    def _fmt_int(value: int) -> str:
        return f"{value:,}"

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        seconds = max(0, int(seconds))
        hours, rest = divmod(seconds, 3600)
        minutes, secs = divmod(rest, 60)
        if hours:
            return f"{hours} 小时 {minutes} 分"
        if minutes:
            return f"{minutes} 分 {secs} 秒"
        return f"{secs} 秒"

    # ------------------------------------------------------------------
    # 循环
    # ------------------------------------------------------------------

    def _start(self, session: str) -> None:
        if self._running:
            return
        if session:
            self._session = session
        self._running = True
        self._runtime = _Runtime(started_at=time.time())
        self._loop_task = asyncio.create_task(self._loop())

    def _cancel_loop(self) -> None:
        self._running = False
        task, self._loop_task = self._loop_task, None
        if task and not task.done():
            task.cancel()

    def _runtime_exceeded(self) -> bool:
        if not self._runtime.started_at:
            return False
        limit = self._limits().max_runtime_minutes * 60
        return time.time() - self._runtime.started_at >= limit

    def _next_wait(self) -> float:
        """间隔再长，也不能越过单次运行上限，否则到点了还在睡。"""
        limits = self._limits()
        wait = float(limits.interval_seconds)
        if self._runtime.started_at:
            elapsed = time.time() - self._runtime.started_at
            left = limits.max_runtime_minutes * 60 - elapsed
            wait = min(wait, max(1.0, left))
        return max(1.0, wait)

    async def _loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(self._next_wait())
                if not self._running:
                    break
                if self._runtime_exceeded():
                    await self._halt(
                        f"到了单次运行上限 {self._limits().max_runtime_minutes} 分钟"
                    )
                    break
                await self._step()
        except asyncio.CancelledError:
            logger.info(f"[{PLUGIN_NAME}] 循环已取消")
            raise
        except Exception as exc:  # pragma: no cover - 兜底，别让插件静默死掉
            logger.error(f"[{PLUGIN_NAME}] 循环异常退出：{exc}")
            self._running = False
        finally:
            self._running = False
            if self._loop_task is asyncio.current_task():
                self._loop_task = None

    async def _step(self) -> None:
        """一次空转：判额度 → 取 provider → 发请求 → 记真账。"""
        limits = self._limits()
        estimated = limits.estimated_tokens

        allowed, why = self.ledger.can_spend(estimated, limits)
        if not allowed:
            await self._halt(f"{why}")
            return

        provider = None
        try:
            provider = await resolve_provider(
                self.context, str(self._cfg("provider_id", ""))
            )
        except Exception as exc:
            self._runtime.last_error = f"取 LLM 失败：{exc}"[:120]
            logger.warning(f"[{PLUGIN_NAME}] {self._runtime.last_error}")
        if provider is None:
            self._runtime.fail += 1
            self._runtime.last_error = "没有可用的 LLM 提供方"
            if self._runtime.fail >= MAX_CONSECUTIVE_FAILURES:
                await self._halt(
                    f"连续 {self._runtime.fail} 次拿不到 LLM，先停了下来"
                )
            return

        timeout = float(
            clamp_int(self._cfg("request_timeout", 60), 5, 300, 60)
        )
        self.ledger.reserve(estimated)
        try:
            result = await burn_once(
                provider,
                prompt=self._prompt(),
                system_prompt=str(self._cfg("system_prompt", "")),
                max_tokens=limits.max_tokens_per_request,
                session_id="worldcodes_burner",
                timeout=timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 失败不退还预占额度：请求可能已经打到服务端，宁可少烧不可漏记
            self._runtime.fail += 1
            self._runtime.last_error = str(exc)[:120]
            logger.warning(f"[{PLUGIN_NAME}] 空转失败：{exc}")
            if self._runtime.fail >= MAX_CONSECUTIVE_FAILURES:
                await self._halt(
                    f"连续 {self._runtime.fail} 次调用失败：{self._runtime.last_error}"
                )
            return

        self.ledger.settle(estimated, result.total_tokens)
        self._runtime.ok += 1
        self._runtime.fail = 0
        self._runtime.last_text = result.text[:40]

    async def _halt(self, reason: str) -> None:
        """无论在循环内还是命令里，都能安全地停下来。"""
        self._running = False
        task = self._loop_task
        if task and task is not asyncio.current_task():
            self._loop_task = None
            task.cancel()
        await self._notify(f"烧 token 已停：{reason}")

    async def _notify(self, text: str) -> None:
        session = self._session or str(self._cfg("notify_session", ""))
        if not session:
            logger.info(f"[{PLUGIN_NAME}] {text}")
            return
        try:
            await self.context.send_message(
                session, MessageChain(chain=[Plain(text)])
            )
        except Exception as exc:  # pragma: no cover - 平台差异
            logger.warning(f"[{PLUGIN_NAME}] 通知发送失败：{exc}")

    # ------------------------------------------------------------------
    # 文本
    # ------------------------------------------------------------------

    def _status_text(self) -> str:
        limits = self._limits()
        snap = self.ledger.snapshot(limits)
        if self._running:
            ran = (
                self._fmt_duration(time.time() - self._runtime.started_at)
                if self._runtime.started_at
                else "刚开始"
            )
            head = f"烧 token：运行中（已跑 {ran}）"
        else:
            head = "烧 token：未运行"

        lines = [
            head,
            f"间隔 {limits.interval_seconds}s · 单次输出 <= {limits.max_tokens_per_request} tokens",
            (
                f"今日 {self._fmt_int(snap['spent_tokens'])} / "
                f"{self._fmt_int(snap['daily_budget_tokens'])} tokens · "
                f"{snap['requests']} / {snap['daily_max_requests']} 次"
            ),
            (
                f"单次运行上限 {limits.max_runtime_minutes} 分钟 · "
                f"提示「{self._prompt()}」"
            ),
        ]
        if self._running:
            lines.append(
                f"本轮 OK {self._runtime.ok} · 失败 {self._runtime.fail}"
            )
            if self._runtime.last_error:
                lines.append(f"最近错误：{self._runtime.last_error}")
        else:
            lines.append("停：/烧token 停（或 /停烧）")
        return "\n".join(lines)

    @staticmethod
    def _help_text() -> str:
        return (
            "烧 token（空转消耗，默认关闭）\n"
            "/烧token          看状态\n"
            "/烧token 开 [间隔秒] [单次tokens]   开启，第一发在间隔之后\n"
            "/烧token 停       立即停止\n"
            "/烧token 帮助     这份说明\n"
            "/停烧             等价于 停\n"
            "硬上限：间隔 "
            f"{MIN_INTERVAL_SECONDS}–{MAX_INTERVAL_SECONDS}s，单次 <= "
            f"{HARD_MAX_TOKENS_PER_REQUEST} tokens，每日 <= "
            f"{HARD_DAILY_BUDGET_TOKENS} tokens / {HARD_DAILY_REQUESTS} 次，"
            f"单次运行 <= {HARD_MAX_RUNTIME_MINUTES} 分钟。\n"
            "配置里 enabled=true 只决定插件加载时是否自动开跑。"
        )

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------

    @filter.command("烧token", alias={"burn", "空转"})
    async def cmd_burn(self, event: AstrMessageEvent):
        """烧token [状态|开 [间隔秒] [单次tokens]|停|帮助]"""
        blocked = self._ready(event)
        if blocked:
            yield event.plain_result(blocked)
            return

        raw = self._strip_command(event.get_message_str(), *_COMMAND_NAMES)
        args = raw.split()
        sub = args[0].lower() if args else ""

        if sub in ("开", "start", "on"):
            if self._running:
                yield event.plain_result("已经在跑了。要换参数先 /烧token 停。")
                return

            numbers: list[int] = []
            for token in args[1:]:
                try:
                    numbers.append(int(float(token)))
                except ValueError:
                    continue

            clamped = False
            if len(numbers) >= 1:
                want = numbers[0]
                self._overrides["interval"] = want
                clamped = clamped or want != clamp_int(
                    want, MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS, 60
                )
            if len(numbers) >= 2:
                want_tokens = numbers[1]
                self._overrides["max_tokens"] = want_tokens
                clamped = clamped or want_tokens != clamp_int(
                    want_tokens, 1, HARD_MAX_TOKENS_PER_REQUEST, 16
                )

            limits = self._limits()
            self._start(event.unified_msg_origin)
            text = (
                "开烧\n"
                f"间隔：{limits.interval_seconds}s\n"
                f"单次输出：<= {limits.max_tokens_per_request} tokens\n"
                f"今日预算：{self._fmt_int(limits.daily_budget_tokens)} tokens / "
                f"{limits.daily_max_requests} 次\n"
                f"单次运行：<= {limits.max_runtime_minutes} 分钟\n"
                "停止：/烧token 停"
            )
            if clamped:
                text += "\n（部分参数超出硬上限，已压到允许范围）"
            yield event.plain_result(text)
            return

        if sub in ("停", "stop", "off"):
            was_running = self._running
            self._cancel_loop()
            self._overrides.clear()
            yield event.plain_result(
                "已停。" if was_running else "本来就没在跑。"
            )
            return

        if sub in ("帮助", "help", "?", "？"):
            yield event.plain_result(self._help_text())
            return

        yield event.plain_result(self._status_text())

    @filter.command("停烧", alias={"burnstop"})
    async def cmd_stop(self, event: AstrMessageEvent):
        """停烧：立即停止空转"""
        blocked = self._ready(event)
        if blocked:
            yield event.plain_result(blocked)
            return
        was_running = self._running
        self._cancel_loop()
        self._overrides.clear()
        yield event.plain_result("已停。" if was_running else "本来就没在跑。")
