"""本地评测控制台：`eval serve` → 弹出浏览器页面，点按钮跑评测、自动刷新看结果。

零新依赖（纯标准库 http.server），只绑 127.0.0.1 本机。页面 = 仪表盘 + 顶部控制条：
- 「快跑（免费）」：脚本车道
- 「真跑（花 token）」：真 Gemini，点击有二次确认
- 运行中实时显示日志尾部，跑完自动刷新仪表盘

    python -m evals serve          # 或仓库根目录 eval serve
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from evals import dashboard

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_LOG = os.path.join(_HERE, "runs", "serve-latest.log")
DEFAULT_PORT = 8377

_state: dict = {"proc": None, "mode": None}
_lock = threading.Lock()


def _running() -> bool:
    p = _state["proc"]
    return p is not None and p.poll() is None


def _start(mode: str) -> bool:
    with _lock:
        if _running():
            return False
        os.makedirs(os.path.dirname(_LOG), exist_ok=True)
        args = [sys.executable, "-m", "evals.runner"]
        if mode == "live":
            args += ["--live", "--out", "evals/report_live.html"]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", REPL_USE_MOCK_DB="1")
        logf = open(_LOG, "w", encoding="utf-8")
        _state["proc"] = subprocess.Popen(args, cwd=_ROOT, env=env,
                                          stdout=logf, stderr=subprocess.STDOUT)
        _state["mode"] = mode
        return True


def _tail(n: int = 14) -> list[str]:
    try:
        with open(_LOG, encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()[-n:]
    except OSError:
        return []


_BAR = """
<div id="ctl" style="position:sticky;top:0;background:#fffdf7;border:1px solid #ece9e0;
 border-radius:10px;padding:10px 14px;margin-bottom:16px;font-size:13px;z-index:9">
 <b>评测控制台</b>
 <button onclick="run('quick')" id="bq" style="margin-left:10px">快跑（免费）</button>
 <button onclick="if(confirm('真跑会调 Gemini、花 token（约 $1，20-30 分钟）。继续？'))run('live')" id="bl">真跑（花 token）</button>
 <button onclick="location.reload()">刷新</button>
 <span id="st" style="margin-left:10px;color:#6b6a66"></span>
 <pre id="log" style="display:none;max-height:180px;overflow:auto;background:#faf9f6;
  border:1px solid #ece9e0;border-radius:6px;padding:8px;font-size:12px;margin:8px 0 0"></pre>
</div>
<script>
var wasRunning=false;
function run(m){fetch('/run/'+m,{method:'POST'}).then(function(){poll();});}
function poll(){fetch('/status').then(function(r){return r.json();}).then(function(s){
  var st=document.getElementById('st'),log=document.getElementById('log');
  document.getElementById('bq').disabled=s.running;document.getElementById('bl').disabled=s.running;
  if(s.running){wasRunning=true;st.textContent='运行中（'+(s.mode==='live'?'真跑':'快跑')+'）…';
    log.style.display='block';log.textContent=s.tail.join('\\n');log.scrollTop=log.scrollHeight;
    setTimeout(poll,2000);}
  else{if(wasRunning){location.reload();}st.textContent='空闲';log.style.display='none';}
});}
poll();
</script>
"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 安静，别刷终端
        pass

    def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/status"):
            self._send(200, json.dumps({"running": _running(), "mode": _state["mode"],
                                        "tail": _tail()}, ensure_ascii=False),
                       "application/json; charset=utf-8")
            return
        dashboard.rebuild()
        with open(dashboard.DASH_PATH, encoding="utf-8") as fh:
            html = fh.read()
        self._send(200, html.replace("<body>", "<body>" + _BAR, 1))

    def do_POST(self):
        if self.path.startswith("/run/"):
            mode = "live" if self.path.endswith("/live") else "quick"
            ok = _start(mode)
            self._send(200 if ok else 409, json.dumps({"started": ok}),
                       "application/json; charset=utf-8")
        else:
            self._send(404, "{}", "application/json; charset=utf-8")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    port = DEFAULT_PORT
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"评测控制台：{url}  （Ctrl+C 退出；只绑本机）")
    if "--no-open" not in argv:
        try:
            os.startfile(url)  # noqa: S606
        except OSError:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
