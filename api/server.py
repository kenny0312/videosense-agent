"""
Stage 10 —— 端到端编排 API。

    POST /v1/video_vibe_query
    Body:  {"query": "自然语言问题"}
    Resp:  {
        ok, answer,
        dag,               # Planner 生成的执行蓝图(可审计)
        generated_code,    # 每个沙箱节点最终版 Python(自愈后)
        plot_url,          # 图表 URL(gs:// 或 file://),无图则 null
        trace, trace_summary
    }

本地启动:
    uvicorn api.server:app --port 8000 --reload
环境变量同 pipeline.main(REPL_USE_MOCK_DB / ALLOYDB_PASSWORD / SANDBOX_URL ...)。

注意:endpoint 用同步 def,FastAPI 自动放线程池执行 —— 避免阻塞事件循环,
也避开与 MCP 客户端后台 loop / Vertex AI 阻塞调用的冲突。
"""
from __future__ import annotations

import uuid
import warnings

warnings.filterwarnings("ignore", category=UserWarning, module="vertexai.*")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="vertexai.*")

import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pipeline import artifacts, config
from pipeline.orchestrator import run_query

app = FastAPI(title="VideoSense Agent", version="1.0")

# 把本地 artifacts/ 目录挂成静态服务 —— 生成的图表用浏览器直接打开
os.makedirs(artifacts.LOCAL_DIR, exist_ok=True)
app.mount("/plots", StaticFiles(directory=artifacts.LOCAL_DIR), name="plots")


# ── 极简前端测试页(GET /) —— 输入框 → 调 /v1/video_vibe_query → 渲染结果 ──
_TEST_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>VideoSense Agent · 测试台</title>
<style>
  :root{color-scheme:dark}
  body{margin:0;background:#0a0a0c;color:#e5e7eb;font:14px/1.6 system-ui,"Segoe UI",sans-serif}
  .wrap{max-width:880px;margin:0 auto;padding:28px 20px}
  h1{font-size:20px;margin:0 0 2px}
  .sub{color:#6b7280;font-size:12px;margin:0 0 18px}
  textarea{width:100%;box-sizing:border-box;background:#111113;color:#e5e7eb;border:1px solid #1f2937;
    border-radius:10px;padding:12px;font:14px/1.5 inherit;resize:vertical}
  .row{display:flex;gap:10px;align-items:center;margin-top:10px}
  button{background:#2563eb;color:#fff;border:0;border-radius:8px;padding:9px 18px;font-size:14px;cursor:pointer}
  button:disabled{opacity:.4;cursor:not-allowed}
  #status{color:#9ca3af;font-size:12px}
  h3{font-size:13px;color:#93c5fd;margin:18px 0 6px}
  pre{background:#111113;border:1px solid #1f2937;border-radius:10px;padding:12px;overflow:auto;font-size:12px;white-space:pre-wrap;word-break:break-word}
  .chips{display:flex;flex-wrap:wrap;gap:6px}
  .chip{background:#1f2937;border-radius:999px;padding:3px 10px;font-size:12px;color:#cbd5e1}
  img{max-width:100%;border:1px solid #1f2937;border-radius:10px;background:#fff}
</style></head>
<body><div class="wrap">
  <h1>🎬 VideoSense Agent · 本地测试台</h1>
  <p class="sub">输入自然语言问题 → 看 答案 / DAG / trace / 图表。Ctrl/Cmd+Enter 发送。</p>
  <textarea id="q" rows="3" placeholder="例如: How many videos are in the database?"></textarea>
  <div class="row"><button id="go">发送</button><span id="status"></span></div>
  <div id="out"></div>
</div>
<script>
const $=id=>document.getElementById(id);
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
const G={ok:'[+]',error:'[x]',retry:'[~]',running:'[ ]'};
async function send(){
  const q=$('q').value.trim(); if(!q) return;
  $('go').disabled=true; $('status').textContent='运行中…(规划→执行,可能十几秒)'; $('out').innerHTML='';
  const t0=performance.now();
  try{
    const r=await fetch('/v1/video_vibe_query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:q})});
    const d=await r.json(); render(d,Math.round(performance.now()-t0));
  }catch(e){ $('status').textContent='请求失败: '+e; }
  $('go').disabled=false;
}
function render(d,ms){
  if(d.status==='smalltalk'){
    $('status').textContent='💬 · '+ms+'ms';
    $('out').innerHTML='<pre>'+esc(d.answer||'')+'</pre>';
    return;
  }
  if(d.status==='refused'){
    $('status').textContent='🛑 无法回答 · '+(d.trace_summary||'')+' · '+ms+'ms';
    $('out').innerHTML='<h3>无法回答</h3><pre>'+esc(d.reason||'')+'</pre>';
    return;
  }
  $('status').textContent=(d.ok?'✅ 成功':'❌ 失败')+' · '+(d.trace_summary||'')+' · '+ms+'ms(含网络)';
  let h='';
  if(d.dag&&d.dag.nodes){h+='<h3>DAG</h3><div class="chips">'+d.dag.nodes.map(n=>'<span class="chip">'+esc(n.id)+': '+esc(n.tool)+'</span>').join('')+'</div>';}
  h+='<h3>答案</h3><pre>'+esc(JSON.stringify(d.ok?d.answer:d.error,null,2))+'</pre>';
  if(d.plot_url){h+='<h3>图表</h3><img src="'+d.plot_url+'"/>';}
  if(d.trace){h+='<h3>Trace</h3><pre>'+d.trace.map(s=>(G[s.status]||'[ ]')+' '+s.name+'  '+s.elapsed_ms+'ms'+(s.error?'  -> '+s.error:'')).join('\\n')+'</pre>';}
  if(d.generated_code){for(const k in d.generated_code){h+='<h3>生成代码 · '+esc(k)+'</h3><pre>'+esc(d.generated_code[k])+'</pre>';}}
  $('out').innerHTML=h;
}
$('go').onclick=send;
$('q').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey))send();});
</script></body></html>"""


class VibeQueryRequest(BaseModel):
    query: str = Field(..., description="自然语言视频分析问题")


@app.get("/", response_class=HTMLResponse)
def index():
    return _TEST_PAGE


@app.get("/health")
def health():
    return {"status": "ok", "mode": "mock" if config.USE_MOCK_DB else "alloydb"}


@app.post("/v1/video_vibe_query")
def video_vibe_query(req: VibeQueryRequest, request: Request):
    result = run_query(req.query, quiet_trace=True)

    # 图表产物:沙箱产出的图像(svg/png)→ 存本地 → 返回浏览器可打开的 http URL
    plot_url = None
    plot = result.pop("plot", {}) or {}
    if plot:
        fname = artifacts.save_local(plot, name=uuid.uuid4().hex[:12])
        if fname:
            plot_url = str(request.base_url).rstrip("/") + f"/plots/{fname}"

    result["plot_url"] = plot_url
    return result
