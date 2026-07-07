"""VS 评测系统（evals）—— 离线脚本车道。

Offline eval harness for VS. Drives the real agent loop (pipeline.run_loop) with a
scripted "brain" + stubbed tools, so verifiable scorers can be tested with no Gemini,
no network, no DB. See evals/README.md.
"""
