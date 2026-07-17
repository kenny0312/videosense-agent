"""P0-2:滥用/账单护栏 —— 速率 + 成本双口径,纵深四维(注入 fake redis,离线)。"""
from pipeline import config
from pipeline.agentops import ratelimit


class _FakeRedis:
    def __init__(self): self.store = {}
    def get(self, k): return self.store.get(k)
    def set(self, k, v, ex=None): self.store[k] = v
    def mget(self, *keys): return [self.store.get(k) for k in keys]
    def incr(self, k):
        self.store[k] = int(self.store.get(k, 0)) + 1
        return self.store[k]
    def incrbyfloat(self, k, amt):
        self.store[k] = float(self.store.get(k, 0.0)) + float(amt)
        return self.store[k]
    def expire(self, k, ttl): pass


def _use(monkeypatch, fake):
    monkeypatch.setattr(ratelimit, "_redis", lambda: fake)
    monkeypatch.setattr(config, "USE_RATE_LIMIT", True)


def test_allows_when_fresh(monkeypatch):
    _use(monkeypatch, _FakeRedis())
    assert ratelimit.precheck("alice", "1.1.1.1", "s1") is None   # 空账 → 放行


def test_user_minute_rate_limit(monkeypatch):
    _use(monkeypatch, _FakeRedis())
    monkeypatch.setattr(config, "RL_REQ_PER_MIN", 3)
    for _ in range(3):
        assert ratelimit.precheck("alice", None, "s1") is None    # 1,2,3 放行
    r = ratelimit.precheck("alice", None, "s1")                   # 第 4 次 → 挡
    assert r is not None and "频繁" in r


def test_daily_cost_cap_user(monkeypatch):
    _use(monkeypatch, _FakeRedis())
    monkeypatch.setattr(config, "RL_DAILY_COST_USD", 1.0)
    ratelimit.record("bob", None, "s1", 0.6)
    ratelimit.record("bob", None, "s1", 0.6)                      # 累计 1.2 ≥ 1.0
    r = ratelimit.precheck("bob", None, "s2")                     # 换会话也拦(按用户当日)
    assert r is not None and "今日" in r


def test_session_cost_cap(monkeypatch):
    _use(monkeypatch, _FakeRedis())
    monkeypatch.setattr(config, "RL_SESSION_COST_USD", 0.5)
    monkeypatch.setattr(config, "RL_DAILY_COST_USD", 999.0)       # 排除日顶干扰
    ratelimit.record("carol", None, "sess-x", 0.7)               # 单会话烧穿
    assert ratelimit.precheck("carol", None, "sess-x") is not None
    assert ratelimit.precheck("carol", None, "sess-y") is None    # 新会话 → 放行


def test_global_daily_circuit_breaker(monkeypatch):
    _use(monkeypatch, _FakeRedis())
    monkeypatch.setattr(config, "RL_GLOBAL_DAILY_COST_USD", 2.0)
    monkeypatch.setattr(config, "RL_DAILY_COST_USD", 999.0)
    ratelimit.record("u1", None, "s1", 1.5)
    ratelimit.record("u2", None, "s2", 1.0)                       # 全站累计 2.5 ≥ 2.0
    r = ratelimit.precheck("u3", None, "s3")                      # 与前两人无关的第三人也被熔断
    assert r is not None and "总用量" in r


def test_guest_tier_is_tighter(monkeypatch):
    _use(monkeypatch, _FakeRedis())
    monkeypatch.setattr(config, "RL_DAILY_COST_USD_GUEST", 0.10)
    monkeypatch.setattr(config, "RL_DAILY_COST_USD", 5.0)         # 具名档很松
    ratelimit.record("anon", None, "s1", 0.15)                    # anon = 小额度档
    assert ratelimit.precheck("anon", None, "s2") is not None     # 撞 guest 日顶
    assert ratelimit.precheck("dave", None, "s3") is None         # 同样花费下具名用户放行


def test_ip_flood_guard_small_tier(monkeypatch):
    _use(monkeypatch, _FakeRedis())
    monkeypatch.setattr(config, "RL_IP_REQ_PER_MIN", 2)
    monkeypatch.setattr(config, "RL_REQ_PER_MIN_GUEST", 999)      # 隔离出 IP 维度
    assert ratelimit.precheck("anon", "9.9.9.9", "s1") is None
    assert ratelimit.precheck("anon", "9.9.9.9", "s2") is None
    r = ratelimit.precheck("anon", "9.9.9.9", "s3")              # 同 IP 第 3 次 → 挡
    assert r is not None and "单地址" in r


def test_failopen_no_redis(monkeypatch):
    monkeypatch.setattr(ratelimit, "_redis", lambda: None)
    monkeypatch.setattr(config, "USE_RATE_LIMIT", True)
    assert ratelimit.precheck("alice", "1.1.1.1", "s1") is None   # 无 Redis → 放行(硬底线在 spend cap)
    ratelimit.record("alice", None, "s1", 1.0)                   # 不崩


def test_disabled_switch(monkeypatch):
    _use(monkeypatch, _FakeRedis())
    monkeypatch.setattr(config, "USE_RATE_LIMIT", False)
    monkeypatch.setattr(config, "RL_REQ_PER_MIN", 1)
    for _ in range(5):
        assert ratelimit.precheck("alice", None, "s1") is None    # 关了就完全不拦
