# -*- coding: utf-8 -*-
"""预算账本与硬上限：给「空转烧 token」装上刹车。

三条原则：

1. 配置值先 clamp 再用。配置文件里写 1 秒、100 万 tokens，实际都会被压到硬上限。
2. 用量落盘。插件重载、进程重启都不清零，堵死「重启绕限额」这条捷径。
3. 按本地日期分桶，跨天自动结转，历史最多留 30 天。

这个文件不依赖 astrbot，可以直接单测。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

# 硬上限：只有改代码才能突破
MIN_INTERVAL_SECONDS = 15
MAX_INTERVAL_SECONDS = 3600
HARD_MAX_TOKENS_PER_REQUEST = 512
MIN_DAILY_BUDGET_TOKENS = 500
HARD_DAILY_BUDGET_TOKENS = 200_000
MIN_DAILY_REQUESTS = 1
HARD_DAILY_REQUESTS = 500
MIN_MAX_RUNTIME_MINUTES = 1
HARD_MAX_RUNTIME_MINUTES = 360
MAX_PROMPT_CHARS = 64
HISTORY_DAYS = 30


def clamp_int(value: Any, low: int, high: int, default: int) -> int:
    """把任意配置值压进 [low, high]；解析失败时用 default。"""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def clamp_prompt(text: str, default: str = "嗯") -> str:
    """空转提示只允许一行、最多 64 字符，防止有人往里塞长文。"""
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        cleaned = default
    return cleaned[:MAX_PROMPT_CHARS]


@dataclass(frozen=True)
class BurnLimits:
    """一次运行生效的全部限额，均已 clamp。"""

    interval_seconds: int = 60
    max_tokens_per_request: int = 16
    daily_budget_tokens: int = 20_000
    daily_max_requests: int = 100
    max_runtime_minutes: int = 30

    @property
    def estimated_tokens(self) -> int:
        """一次请求的保守预估：输出上限 + 提示与系统消息的固定开销。"""
        return self.max_tokens_per_request + 64

    @classmethod
    def from_config(
        cls,
        config: dict | None,
        *,
        interval: Any = None,
        max_tokens: Any = None,
    ) -> "BurnLimits":
        raw = config or {}

        def pick(key: str, default: Any) -> Any:
            value = raw.get(key, default)
            return default if value in (None, "") else value

        return cls(
            interval_seconds=clamp_int(
                pick("interval_seconds", 60) if interval in (None, "") else interval,
                MIN_INTERVAL_SECONDS,
                MAX_INTERVAL_SECONDS,
                60,
            ),
            max_tokens_per_request=clamp_int(
                pick("max_tokens_per_request", 16)
                if max_tokens in (None, "")
                else max_tokens,
                1,
                HARD_MAX_TOKENS_PER_REQUEST,
                16,
            ),
            daily_budget_tokens=clamp_int(
                pick("daily_budget_tokens", 20_000),
                MIN_DAILY_BUDGET_TOKENS,
                HARD_DAILY_BUDGET_TOKENS,
                20_000,
            ),
            daily_max_requests=clamp_int(
                pick("daily_max_requests", 100),
                MIN_DAILY_REQUESTS,
                HARD_DAILY_REQUESTS,
                100,
            ),
            max_runtime_minutes=clamp_int(
                pick("max_runtime_minutes", 30),
                MIN_MAX_RUNTIME_MINUTES,
                HARD_MAX_RUNTIME_MINUTES,
                30,
            ),
        )


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class BudgetLedger:
    """按天记账的 token 预算，落盘为单个 JSON。

    结构::

        {
          "day": "2026-06-01",
          "spent_tokens": 1024,
          "requests": 12,
          "total_spent_tokens": 20480,
          "total_requests": 240,
          "history": [{"day": "2026-05-31", "spent_tokens": 900, "requests": 10}]
        }
    """

    def __init__(
        self,
        path: Path,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self._now = now
        self._data = self._fresh()
        self.load()

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------

    def _fresh(self) -> dict[str, Any]:
        return {
            "day": self._today(),
            "spent_tokens": 0,
            "requests": 0,
            "total_spent_tokens": 0,
            "total_requests": 0,
            "history": [],
        }

    def _today(self) -> str:
        return datetime.fromtimestamp(self._now()).date().isoformat()

    def load(self) -> None:
        if not self.path.exists():
            self._data = self._fresh()
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            # 文件坏了就当新账本，不让插件起不来
            self._data = self._fresh()
            return
        if not isinstance(raw, dict):
            self._data = self._fresh()
            return
        history = raw.get("history")
        self._data = {
            "day": str(raw.get("day") or self._today()),
            "spent_tokens": max(0, _as_int(raw.get("spent_tokens"))),
            "requests": max(0, _as_int(raw.get("requests"))),
            "total_spent_tokens": max(0, _as_int(raw.get("total_spent_tokens"))),
            "total_requests": max(0, _as_int(raw.get("total_requests"))),
            "history": history[-HISTORY_DAYS:] if isinstance(history, list) else [],
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2, default=str),
            "utf-8",
        )
        tmp.replace(self.path)

    # ------------------------------------------------------------------
    # 跨天结转
    # ------------------------------------------------------------------

    def rollover(self) -> bool:
        """日期变了就把当天用量归档并清零，返回是否发生结转。"""
        today = self._today()
        if self._data.get("day") == today:
            return False
        spent = _as_int(self._data.get("spent_tokens"))
        requests = _as_int(self._data.get("requests"))
        if spent or requests:
            history = self._data.setdefault("history", [])
            history.append(
                {
                    "day": self._data.get("day"),
                    "spent_tokens": spent,
                    "requests": requests,
                }
            )
            self._data["history"] = history[-HISTORY_DAYS:]
        self._data["day"] = today
        self._data["spent_tokens"] = 0
        self._data["requests"] = 0
        self.save()
        return True

    # ------------------------------------------------------------------
    # 预算判定
    # ------------------------------------------------------------------

    def remaining_tokens(self, limits: BurnLimits) -> int:
        self.rollover()
        return max(0, limits.daily_budget_tokens - _as_int(self._data["spent_tokens"]))

    def remaining_requests(self, limits: BurnLimits) -> int:
        self.rollover()
        return max(0, limits.daily_max_requests - _as_int(self._data["requests"]))

    def can_spend(self, estimated_tokens: int, limits: BurnLimits) -> tuple[bool, str]:
        """返回 (能否再烧一次, 不能的原因)。"""
        self.rollover()
        spent = _as_int(self._data["spent_tokens"])
        requests = _as_int(self._data["requests"])
        if requests >= limits.daily_max_requests:
            return False, f"今日请求次数到顶（{requests}/{limits.daily_max_requests}）"
        left = limits.daily_budget_tokens - spent
        if left < estimated_tokens:
            return False, (
                f"今日预算只剩 {max(0, left)} tokens，"
                f"不够下一次预估的 {estimated_tokens}"
            )
        return True, ""

    # ------------------------------------------------------------------
    # 记账
    # ------------------------------------------------------------------

    def reserve(self, estimated_tokens: int) -> None:
        """请求发出前先占额度，避免中途崩溃时漏记。"""
        self.rollover()
        estimated_tokens = max(0, int(estimated_tokens))
        self._data["spent_tokens"] = _as_int(self._data["spent_tokens"]) + estimated_tokens
        self._data["requests"] = _as_int(self._data["requests"]) + 1
        self._data["total_spent_tokens"] = (
            _as_int(self._data["total_spent_tokens"]) + estimated_tokens
        )
        self._data["total_requests"] = _as_int(self._data["total_requests"]) + 1
        self.save()

    def settle(self, estimated_tokens: int, actual_tokens: int) -> None:
        """拿到真实用量后修正预占；差额可能为负。"""
        self.rollover()
        delta = max(0, int(actual_tokens)) - max(0, int(estimated_tokens))
        if delta == 0:
            return
        self._data["spent_tokens"] = max(
            0, _as_int(self._data["spent_tokens"]) + delta
        )
        self._data["total_spent_tokens"] = max(
            0, _as_int(self._data["total_spent_tokens"]) + delta
        )
        self.save()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def snapshot(self, limits: BurnLimits) -> dict[str, Any]:
        self.rollover()
        return {
            "day": self._data["day"],
            "spent_tokens": _as_int(self._data["spent_tokens"]),
            "requests": _as_int(self._data["requests"]),
            "daily_budget_tokens": limits.daily_budget_tokens,
            "daily_max_requests": limits.daily_max_requests,
            "remaining_tokens": self.remaining_tokens(limits),
            "remaining_requests": self.remaining_requests(limits),
            "total_spent_tokens": _as_int(self._data["total_spent_tokens"]),
            "total_requests": _as_int(self._data["total_requests"]),
        }
