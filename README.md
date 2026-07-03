<div align="center">

<img src="docs/hero.svg" alt="VideoSense — ask your video library anything; it answers with proof" width="100%" />

<br/><br/>

**English** · [简体中文](README.zh-CN.md)

### Your videos hold answers. VideoSense finds them —<br/>it watches, reasons, and replies with the clip and the chart to prove it.

</div>

<br/>

<div align="center">
  <img src="docs/demo.gif" alt="VideoSense in action — a question becomes an answer with playable clips" width="820" />
</div>

<br/>

## 💬 You ask · it answers

| You ask… | …you get |
|:---|:---|
| 🪂 &nbsp;*“How many wingsuit videos are there?”* | **“12”** — it watched & phase-tagged each one |
| 🎬 &nbsp;*“Show me the shortest clip”* | plays it **inline** |
| 🔎 &nbsp;*“Which clips show only freefall, no parachute?”* | a filtered list, each **playable** |
| 📊 &nbsp;*“Plot the confidence distribution”* | a **chart** |
| 💬 &nbsp;*“List a few more · how did you get that?”* | **remembers** the conversation & continues |

<br/>

## ✨ Why it feels different

<table>
<tr>
<td width="33%" valign="top" align="center"><br/>💬<h3>Just ask</h3><sub>No SQL, no dashboards.<br/>Plain language in.</sub><br/><br/></td>
<td width="33%" valign="top" align="center"><br/>🧠<h3>It actually watches</h3><sub>Gemini multimodal reads the<br/>video — not just metadata.</sub><br/><br/></td>
<td width="33%" valign="top" align="center"><br/>🎬<h3>Answers you can see</h3><sub>The answer + the clip + the<br/>chart, and how it got there.</sub><br/><br/></td>
</tr>
</table>

<br/>

<div align="center">

### 🚀 Try it in 30 seconds — free mock mode

</div>

```bash
export GCP_PROJECT="your-gcp-project"  REPL_USE_MOCK_DB=1
uvicorn api.server:app --port 8000        # then open http://localhost:8000
```

<sub>No database, no cost — a built-in sample library. You only need <code>gcloud auth application-default login</code> for Gemini.</sub>

<br/>

## 🧠 How it answers

There is no pre-baked pipeline. An agent loop with **Gemini 2.5** as the brain decides its own next move — watch a video, query the facts it has extracted, search semantically, run a calculation, draw a chart — and keeps going until it can *prove* an answer, streaming every step back live. It remembers you across sessions, meters its own cost per request, and runs in production on Cloud Run with **146 tests** behind it.

<sub>Curious about the internals? Architecture notes live in [`docs/design/`](docs/design/).</sub>

<br/>

<div align="center">

<sub>Built by <a href="https://kenny0312.github.io">Kenny Qiu</a> &nbsp;·&nbsp; see also <a href="https://github.com/kenny0312/social-video-insights">SocialLens</a>, a social-video insights demo &nbsp;·&nbsp; <a href="README.zh-CN.md">简体中文</a></sub>

</div>
