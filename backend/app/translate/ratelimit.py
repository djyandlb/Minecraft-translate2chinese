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
# v1.3.8（用户「DeepSeek 2500 并发测出 72」）：auto 上限 300 太低——并发型 API
#（DeepSeek V4 官方 2500 并发、无 RPM 限流）线性爬坡到 64 并发、单批 1s 需 3840 RPM，
# 闸上限 300 会把并发能力卡死（高并发 API 测不到顶的次生瓶颈）。抬到 10000：
# 并发型 API 不再被闸限速（靠全局并发池 _conc_sem 控并发）；RPM 型 API（MiniMax/百炼）
# 爬坡时撞 429 自动退档，闸仍保护不超配额。10000 仅是「够 64 并发猛发」的兜底，非鼓励。
_AUTO_MAX_RPM = 10000.0  # 自动校准上限（v1.3.8 300→10000：并发型 API 不被闸卡死）
_AUTO_BACKOFF = 0.6       # 撞 429 退幅（×0.6）
_AUTO_RAMPUP = 1.15       # 稳定后微升（×1.15）
_AUTO_OK_STREAK = 15      # 连续成功批次达到后微升一次（v1.2.9：50→15 爬坡加快）——
                          # RateGate 请求前限速保证永不 429，auto 撞不到 report_ratelimit，
                          # 只能靠成功累计升档；原 50 批在翻译 1000 条（25 批）内永不升，
                          # auto 卡死 30 RPM。15 批升 15% → 每 600 条升一次，逼近真实配额。
                          # 主路径仍是动态测试校准值起步（auto_init_rpm），此为无校准兜底。


class RateGate:
    """Token Bucket 请求门：`await gate.acquire()` 通过才允许发一个请求。

    - 桶以当前目标速率每秒补充令牌，容量 = 短突发配额（≈6 秒配额）；
    - 满桶即放行（低流量零延迟），空桶本地等下一令牌；
    - auto 模式：目标速率由 report_ok/report_429 动态校准（撞限流退、稳定升）。
    """

    __slots__ = ("rpm", "auto", "_rate", "_cap", "_tokens", "_last", "_target", "_ok_streak",
                 "_min_cap")

    def __init__(self, rpm: float = 0.0, auto: bool | None = None, auto_init_rpm: float = 0.0,
                 min_cap: int = 1):
        rpm = float(max(0.0, rpm))
        self.auto = auto if auto is not None else (rpm <= 0)
        self.rpm = rpm if not self.auto else 0.0
        # v1.2.9：auto 初始目标 = 校准值（动态测试测得该 API ≈80）>0 时用它起步，否则
        # 保守 _AUTO_INIT_RPM(30)——原固定 30 在翻译 1000 条（<50 批，未达升档门槛）时
        # 永不升档，动态测试校准白测（用户实测「翻译慢」的根因之一）
        self._target = (float(auto_init_rpm) if self.auto and float(auto_init_rpm or 0) > 0
                        else _AUTO_INIT_RPM if self.auto else self.rpm)
        # v1.2.9 关键：桶容量 ≥ 并发数（min_cap）——原 cap = RPM/10（6 秒配额），RPM=30
        # 时只有 3 个令牌突发 → 并发 16 全堵在 acquire 排队、实际同时飞 ≤3（压测实测
        # peak=3，用户「×16 显示但串行」根因）。cap 抬到并发数，让并发请求能突发；
        # auto 模式撞 429 仍会退 target（cap 跟着小），自适应安全。
        self._min_cap = max(1, int(min_cap))
        self._ok_streak = 0
        self._reset_bucket()

    def _reset_bucket(self) -> None:
        # 修复（recheck）：rpm=0 + auto=False 时 _target=0 → _rate=0，acquire() 超配额
        # sleep 除零抛 ZeroDivisionError——取极小速率兜底（实际路径 rpm<=0 走 auto，防御公共类）
        self._rate = max(1e-9, self._target / 60.0)
        # v1.2.9：桶容量 ≥ min_cap（并发数）——并发突发不被令牌桶憋死（auto 模式撞 429 会退档自愈）
        # 修复（Agent recheck）：**固定模式（auto=False）突发不超 RPM 配额**——cap 钳
        # min(min_cap, RPM)，否则 RPM=15/并发=16 时 burst 16 > 整个 60s 窗口配额必撞 429，
        # 且固定模式 report_ratelimit 直接 return 无自愈 → 持续撞。
        _cap_burst = (min(float(self._min_cap), self._target) if not self.auto
                      else float(self._min_cap))
        self._cap = max(1.0, _cap_burst, self._target / 10.0)
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
        # v1.2.9 修复（Agent recheck）：退档后**清空桶令牌**——_reset_bucket 满桶 + min_cap
        # 会让退档后立即放行 min_cap 个请求（又撞 429，冷却失效）。空桶 → 按新速率补充
        #（target=1 时约 60s 才放 1 个，真正冷却）。
        self._tokens = 0.0

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