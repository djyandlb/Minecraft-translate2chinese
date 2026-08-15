# -*- coding: utf-8 -*-
"""请求前 RPM 预算闸（v1.2.4，业界标准：LiteLLM / OpenAI SDK 同款 token bucket 思路）。

痛点：MiniMax / 百炼等 API 有 RPM（每分钟请求数）配额。高并发全速猛发 → 超配额 → 429 →
退避重试 → 降档 → 卡（「20 分钟 1000 条」根因之一）。

预算闸在**请求发出前**按配额放行：配额内的请求直接发；超配额的本地排队（睡到下一令牌），
绝不把超配额请求发给 API → API 永不触发 429 → 不重试、不降档，匀速跑满配额给的速度。

两种模式（v1.2.4b）：
- **自动校准（默认，rpm<=0 或 auto=True）**：用户不用知道 API 的 RPM 配额——闸自学习：
  撞到一次 429 → 速率退 40%；连续 50 批成功 → 微升 15%（封顶 300 RPM）。慢慢逼近该 API
  「刚好不触发限流」的满速，永不手动设置。
- **固定（rpm>0）**：知情用户精确控制，按填写的配额放行。

单桶：翻译 / 审查 / 硬编码判断共享同一预算（避免各自超出合计超配额）。
非阻塞：acquire() 用 asyncio.sleep 同步事件循环外的真实等待（asyncio 协程安全）。
"""
import asyncio
import time

_AUTO_INIT_RPM = 30.0     # 自动校准起点（保守，绝不出发即撞限流）
_AUTO_MAX_RPM = 300.0     # 自动校准上限（防失控打到天价用量）
_AUTO_BACKOFF = 0.6       # 撞 429 退幅（×0.6）
_AUTO_RAMPUP = 1.15       # 稳定后微升（×1.15）
_AUTO_OK_STREAK = 50      # 连续成功批次达到后微升一次


class RateGate:
    """Token Bucket 请求门：`await gate.acquire()` 通过才允许发一个请求。

    - 桶以当前目标速率每秒补充令牌，容量 = 短突发配额（≈6 秒配额）；
    - 满桶即放行（低流量零延迟），空桶本地等下一令牌；
    - auto 模式：目标速率由 report_ok/report_429 动态校准（撞限流退、稳定升）。
    """

    __slots__ = ("rpm", "auto", "_rate", "_cap", "_tokens", "_last", "_target", "_ok_streak")

    def __init__(self, rpm: float = 0.0, auto: bool | None = None):
        rpm = float(max(0.0, rpm))
        self.auto = auto if auto is not None else (rpm <= 0)
        self.rpm = rpm if not self.auto else 0.0
        self._target = _AUTO_INIT_RPM if self.auto else self.rpm
        self._ok_streak = 0
        self._reset_bucket()

    def _reset_bucket(self) -> None:
        # 修复（recheck）：rpm=0 + auto=False 时 _target=0 → _rate=0，acquire() 超配额
        # sleep 除零抛 ZeroDivisionError——取极小速率兜底（实际路径 rpm<=0 走 auto，防御公共类）
        self._rate = max(1e-9, self._target / 60.0)
        self._cap = max(1.0, self._target / 10.0)
        self._tokens = self._cap
        self._last = time.monotonic()

    def current_rpm(self) -> float:
        """当前生效的目标速率（auto 模式下会随校准变化）。"""
        return self._target

    def report_ok(self) -> None:
        """一次请求成功（非限流）。auto 模式：连续成功达标 → 微升试探。"""
        if not self.auto:
            return
        self._ok_streak += 1
        if self._ok_streak >= _AUTO_OK_STREAK:
            self._ok_streak = 0
            if self._target < _AUTO_MAX_RPM:
                self._target = min(_AUTO_MAX_RPM, self._target * _AUTO_RAMPUP)
                self._reset_bucket()

    def report_ratelimit(self) -> None:
        """一次请求撞 429/限流。auto 模式：立即退 40%（冷却），稳在「刚好不触发」。"""
        if not self.auto:
            return
        self._target = max(1.0, self._target * _AUTO_BACKOFF)
        self._ok_streak = 0
        self._reset_bucket()

    async def acquire(self) -> None:
        """等待并消耗 1 个令牌。超配额时本地睡到下一令牌，不发请求给 API。"""
        while True:
            now = time.monotonic()
            self._tokens = min(self._cap, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            await asyncio.sleep((1.0 - self._tokens) / self._rate)