# -*- coding: utf-8 -*-
"""请求前 RPM 预算闸（v1.2.4，业界标准：LiteLLM / OpenAI SDK 同款 token bucket 思路）。

痛点：MiniMax / 百炼等 API 有 RPM（每分钟请求数）配额。高并发全速猛发 → 超配额 → 429 →
退避重试 → 降档 → 卡（「20 分钟 1000 条」根因之一）。

预算闸在**请求发出前**按配额放行：配额内的请求直接发；超配额的本地排队（睡到下一令牌），
绝不把超配额请求发给 API → API 永不触发 429 → 不重试、不降档，匀速跑满配额给的速度。

两种模式（v1.2.4b）：
- **自动校准（默认，rpm<=0 或 auto=True）**：用户不用知道 API 的 RPM 配额——闸自学习：
  撞到一次 429 → 速率退 70%（业界保守策略）；连续 15 批成功 → 微升 10%。慢慢逼近该 API
  「刚好不触发限流」的满速，永不手动设置。
- **固定（rpm>0）**：知情用户精确控制，按填写的配额放行。

单桶：翻译 / 审查 / 硬编码判断共享同一预算（避免各自超出合计超配额）。
非阻塞：acquire() 用 asyncio.sleep 同步事件循环外的真实等待（asyncio 协程安全）。

v1.4.5 改进（业界最佳实践）：
- 设置 RPM 下限 10，防止连续限流后降到1导致每60秒才能发1个请求
- 退档更保守（70%而不是60%），升档更慢（10%而不是15%），更稳定
- 尊重 Retry-After 头（如果API返回）

v1.4.7 改进（业界标杆 async-batch-llm / 阿里云限流最佳实践的共享协调器模式）：
- **桶容量收紧到 6 秒配额**（cap = min(并发, RPM/10)）——原 cap=并发数导致桶满
  突发 64 个请求超速，RPM 测了也白测（用户「RPM 测出来为啥还限流」根因）
- **429 全局协调冷却**：任一请求撞 429 → RateGate 设置冷却窗口，所有 acquire 暂停
  等待（共享协调器），冷却后按新速率慢启动——不再各自退避重试风暴
- **固定模式也学习**：固定 RPM 撞 429 同样冷却（固定值高于实际配额时自愈）
"""
import asyncio
import time

_AUTO_INIT_RPM = 30.0     # 自动校准起点（保守，绝不出发即撞限流）
_AUTO_MAX_RPM = 10000.0   # 自动校准上限（并发型 API 不被闸卡死）
_AUTO_MIN_RPM = 50.0      # v1.4.6：RPM 下限50，照顾低端API
_AUTO_BACKOFF = 0.7       # v1.4.5：撞 429 退幅（×0.7，比0.6更保守）
_AUTO_RAMPUP = 1.1        # v1.4.5：稳定后微升（×1.1，比1.15更慢更稳）
_AUTO_OK_STREAK = 15      # 连续成功批次达到后微升一次
_AUTO_RAMPUP_MAX = 300.0  # 自动校准软上限（超过后升档更慢）
# v1.4.7 协调冷却参数（业界：429 cooldown + backoff_multiplier，封顶）
_COOLDOWN_INIT = 5.0      # 撞 429 首次冷却（秒）
_COOLDOWN_MAX = 60.0      # 冷却封顶（尊重 Retry-After 上限）


class RateGate:
    """Token Bucket 请求门：`await gate.acquire()` 通过才允许发一个请求。

    - 桶以当前目标速率每秒补充令牌，容量 = 短突发配额（≈6 秒配额，不超并发）；
    - 满桶即放行（低流量零延迟），空桶本地等下一令牌；
    - **共享协调器**：撞 429 → 全局冷却窗口，所有 acquire 暂停，冷却后慢启动。
    """

    __slots__ = ("rpm", "auto", "_rate", "_cap", "_tokens", "_last", "_target", "_ok_streak",
                 "_min_cap", "_cooldown_until", "_cooldown")

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
        self._min_cap = max(1, int(min_cap))
        self._ok_streak = 0
        self._cooldown_until = 0.0     # 全局冷却截止时间（monotonic）
        self._cooldown = _COOLDOWN_INIT  # 当前冷却时长（下次撞 429 翻倍）
        self._reset_bucket()

    def _reset_bucket(self) -> None:
        # 修复（recheck）：rpm=0 + auto=False 时 _target=0 → _rate=0，acquire() 超配额
        # sleep 除零抛 ZeroDivisionError——取极小速率兜底（实际路径 rpm<=0 走 auto，防御公共类）
        self._rate = max(1e-9, self._target / 60.0)
        # v1.4.7 收紧桶容量：cap = min(并发, 6 秒配额 RPM/10)——原 cap=并发数(64) 导致
        # 桶满突发 64 请求超速触发 API 限流（RPM 测了也白测）。6 秒配额防突发，速率稳定 ≤ RPM。
        # 并发型 API（官方几乎不限流）测出 RPM 高 → cap 也大，不影响并发。
        self._cap = max(1.0, min(float(self._min_cap), self._target / 10.0))
        self._tokens = self._cap
        self._last = time.monotonic()

    def current_rpm(self) -> float:
        """当前生效的目标速率（auto 模式下会随校准变化）。"""
        return self._target

    def report_ok(self) -> None:
        """一次请求成功（非限流）。auto 模式：连续成功达标 → 微升试探。

        v1.4.5：超过300 RPM后升档更慢（×1.05而不是×1.1），防止并发型API被闸卡死。
        """
        # 成功也清零冷却（恢复后不再暂停）
        self._cooldown_until = 0.0
        if not self.auto:
            return
        self._ok_streak += 1
        if self._ok_streak >= _AUTO_OK_STREAK:
            self._ok_streak = 0
            if self._target < _AUTO_MAX_RPM:
                # 超过软上限后升档更慢（×1.05），防止并发型API被闸卡死
                rampup = _AUTO_RAMPUP if self._target < _AUTO_RAMPUP_MAX else 1.05
                self._target = min(_AUTO_MAX_RPM, self._target * rampup)
                self._reset_bucket()

    def report_ratelimit(self, retry_after: float | None = None) -> None:
        """撞 429/限流 → **全局协调冷却**（业界共享协调器模式）。

        - 设置冷却窗口：Retry-After 优先，否则退避（5s 起，翻倍封顶 60s）
        - 冷却期间所有 acquire 暂停等待（不再各自退避重试风暴）
        - auto 模式退档（×0.7）；固定模式也冷却（固定值高于实际配额时自愈）
        - 尊重 Retry-After：按头里的秒数反算 RPM
        """
        now = time.monotonic()
        if retry_after and retry_after > 0:
            cooldown = min(float(retry_after), _COOLDOWN_MAX)
            self._target = max(_AUTO_MIN_RPM, 60.0 / retry_after)
        else:
            cooldown = self._cooldown
            self._target = max(_AUTO_MIN_RPM, self._target * _AUTO_BACKOFF)
        self._cooldown_until = now + cooldown
        self._cooldown = min(self._cooldown * 2.0, _COOLDOWN_MAX)  # 下次翻倍封顶
        self._ok_streak = 0
        self._reset_bucket()
        self._tokens = 0.0

    async def acquire(self) -> None:
        """等待并消耗 1 个令牌。超配额/冷却时本地等待，不发请求给 API。"""
        while True:
            now = time.monotonic()
            # 全局协调冷却：任一请求撞 429 后，所有 acquire 暂停等待窗口
            if self._cooldown_until > now:
                await asyncio.sleep(self._cooldown_until - now)
                continue
            self._tokens = min(self._cap, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            await asyncio.sleep((1.0 - self._tokens) / self._rate)