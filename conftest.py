"""pytest 共享夹具。

M7 起默认执行器是 loop;但 loop 路径要打真 Gemini,无法离线单测。dag 路径(仍作为
`VS_EXECUTOR=dag` 回退保留)的编排单测——followup/meta/拒答门、register_artifact、
recipe 等——通过这里把每个测试钉到 dag 执行器来继续覆盖。loop 路径由
test_loop_driver / test_loop_memory / test_transcript_store + spike/smoke 覆盖。
"""
import pytest

from pipeline import config


@pytest.fixture(autouse=True)
def _force_dag_executor(monkeypatch):
    monkeypatch.setattr(config, "VS_EXECUTOR", "dag")
