"""
SandboxClient 鉴权 / 惰性取令牌的离线单测 —— 不连网、不连真沙箱。
    python -m sandbox.test_client

覆盖:localhost 不取令牌、显式 token 不 fetch、惰性+缓存(只 fetch 一次)、
取令牌优先 google-auth(Cloud Run metadata)、google-auth 失败退化 gcloud。
"""
from __future__ import annotations

import sys

import google.oauth2.id_token as gidt          # patch 点:fetch_id_token
from sandbox.client import SandboxClient


def test_localhost_needs_no_auth():
    c = SandboxClient(url="http://localhost:8080")
    assert c._needs_auth() is False
    assert c._bearer() == ""                     # 不取令牌
    assert c._auth_resolved is True


def test_runapp_injected_token_skips_fetch():
    c = SandboxClient(url="https://sandbox-x.run.app", token="injected")
    assert c._needs_auth() is True
    c._fetch_token = lambda: (_ for _ in ()).throw(AssertionError("不该 fetch"))
    assert c._bearer() == "injected"             # 有显式 token → 不 fetch


def test_bearer_is_lazy_and_cached():
    c = SandboxClient(url="https://sandbox-x.run.app")
    calls = []
    c._fetch_token = lambda: (calls.append(1), "tok")[1]
    assert c._bearer() == "tok"
    assert c._bearer() == "tok"                  # 第二次走缓存
    assert len(calls) == 1                       # 只 fetch 一次


def test_fetch_token_prefers_google_auth():
    c = SandboxClient(url="https://sandbox-x.run.app")
    orig = gidt.fetch_id_token
    gidt.fetch_id_token = lambda req, aud: "GA_TOKEN"
    c._fetch_gcloud_token = lambda: "GCLOUD_TOKEN"   # 不该用到
    try:
        assert c._fetch_token() == "GA_TOKEN"
    finally:
        gidt.fetch_id_token = orig


def test_fetch_token_falls_back_to_gcloud():
    c = SandboxClient(url="https://sandbox-x.run.app")
    orig = gidt.fetch_id_token

    def boom(req, aud):
        raise RuntimeError("no metadata server / no ADC id-token")

    gidt.fetch_id_token = boom
    c._fetch_gcloud_token = lambda: "GCLOUD_TOKEN"
    try:
        assert c._fetch_token() == "GCLOUD_TOKEN"
    finally:
        gidt.fetch_id_token = orig


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
