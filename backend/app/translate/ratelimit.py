# -*- coding: utf-8 -*-
"""请求前 RPM 预算闸（v1.2.4，业界标准：LiteLLM / OpenAI SDK 同款 token bucket 思路）。

痛点：MiniMax / 百炼等 API 有 RPM（每分钟请求数）配额。高并发全速猛发 → 超配额 → 429 →
退避重试 → 降档 → 卡（「20 分钟 1000 条」根因之一）。

预算闸在**请求发出前**按配额放行：配额内的请求直接发；超配额的本地排队（睡到下一令牌），
绝不把超配额请求发给 API → API 永不触发 429 → 不重试、不降档，匀速跑满配额给的速度。

- `rpm<=0`：不限速（RateGate 不创建，栅栏逻辑零开销）。
- 单桶：翻译 / 审查 / 硬编码判断共享同一预算（避免各自超出合计超配额）。
- 非阻塞：acquire() 用 asyncio.sleep 同步事件循环外的真实等待（asyncio 协程安全）。
"""
import asyncio
import time


class RateGate:
    """Token Bucket 请求门：`await gate.acquire()` 通过才允许发一个请求。

    - 桶以 `rpm/60` 每秒速率补充令牌，容量 = 短突发配额（≈6 秒配额），
      允许小幅突发（多并发 task 同时拿几个令牌）而不跳变；
    - 满桶即放行（低流量时零延迟，不排队）；
    - 空桶必须等下一令牌（等待 = max(0, 缺口/补充速率)），本地排队不发给 API。
    """

    __slots__ = ("rpm", "_rate", "_cap", "_tokens", "_last")

    def __init__(self, rpm: float):
        rpm = float(max(1.0, rpm))
        self.rpm = rpm
        self._rate = rpm / 60.0                    # 每秒补充令牌数
        self._cap = max(1.0, rpm / 10.0)           # 突发桶容量 ≈ 6 秒配额
        self._tokens = self._cap                    # 初始满桶（首次请求不等待）
        self._last = time.monotonic()

    async def acquire(self) -> None:
        """等待并消耗 1 个令牌。超配额时本地睡到下一令牌，不发请求给 API。"""
        while True:
            now = time.monotonic()
            self._tokens = min(self._cap, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            # 空桶：睡到积攒出 1 个令牌为止
            await asyncio.sleep((1.0 - self._tokens) / self._rate)