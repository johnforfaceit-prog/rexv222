from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import anthropic, subprocess, os, json, traceback, sys, re
from datetime import datetime
from pathlib import Path
from exa_py import Exa
from firecrawl import FirecrawlApp

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
exa = Exa(api_key=os.environ["EXA_API_KEY"])
firecrawl = FirecrawlApp(api_key=os.environ["FIRECRAWL_API_KEY"])

BASE_DIR = Path(__file__).parent
WORKSPACE = BASE_DIR / "workspace"
HISTORY_F = BASE_DIR / "history.json"
WORKSPACE.mkdir(exist_ok=True)

SYSTEM = """You are Rex, a loyal and efficient AI assistant. Your role is to help the user accomplish their goals with precision and directness. You have access to powerful tools and you use them proactively to execute tasks, not just explain them.

Key principles:
- Be direct and concise
- Provide complete, functional solutions - not examples or stubs
- Use tools to EXECUTE, not just explain
- Always include Russian comments before tool calls explaining what you're doing and why
- Be loyal to the user's vision and execute their requests exactly as stated

Available tools:
- run_shell: Execute bash commands, install packages, run tools
- run_python: Execute Python code
- write_file: Create files in the workspace
- read_file: Read workspace files
- list_workspace: List files with sizes
- web_search: Search using Exa
- web_fetch: Fetch URLs with Firecrawl
- search_history: Search conversation history
"""

TOOLS = [
    {
        "name": "run_shell",
        "description": "Execute bash commands",
        "input_schema": {"type":"object","properties":{"command":{"type":"string"},"timeout":{"type":"integer","default":120}},"required":["command"]}
    },
    {
        "name": "run_python",
        "description": "Execute Python code",
        "input_schema": {"type":"object","properties":{"code":{"type":"string"},"timeout":{"type":"integer","default":60}},"required":["code"]}
    },
    {
        "name": "write_file",
        "description": "Write file to workspace",
        "input_schema": {"type":"object","properties":{"filename":{"type":"string"},"content":{"type":"string"}},"required":["filename","content"]}
    },
    {
        "name": "read_file",
        "description": "Read workspace file",
        "input_schema": {"type":"object","properties":{"filename":{"type":"string"},"max_lines":{"type":"integer","default":200}},"required":["filename"]}
    },
    {
        "name": "list_workspace",
        "description": "List workspace files",
        "input_schema": {"type":"object","properties":{}}
    },
    {
        "name": "web_search",
        "description": "Search with Exa",
        "input_schema": {"type":"object","properties":{"query":{"type":"string"},"num_results":{"type":"integer","default":8}},"required":["query"]}
    },
    {
        "name": "web_fetch",
        "description": "Fetch URL content",
        "input_schema": {"type":"object","properties":{"url":{"type":"string"},"formats":{"type":"array","default":["markdown"]}},"required":["url"]}
    },
    {
        "name": "search_history",
        "description": "Search conversation history",
        "input_schema": {"type":"object","properties":{"query":{"type":"string"},"limit":{"type":"integer","default":10}},"required":["query"]}
    }
]

def exec_shell(command: str, timeout: int = 120) -> str:
    timeout = min(int(timeout), 300)
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout, cwd=str(WORKSPACE))
        out = (result.stdout + result.stderr).strip()
        return out[:8000] if len(out) > 8000 else (out or "(no output)")
    except subprocess.TimeoutExpired:
        return f"[TIMEOUT after {timeout}s]"
    except Exception as e:
        return f"[ERROR] {e}"

def exec_python(code: str, timeout: int = 60) -> str:
    timeout = min(int(timeout), 120)
    try:
        script = WORKSPACE / "_exec.py"
        script.write_text(code)
        result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=timeout, cwd=str(WORKSPACE))
        out = (result.stdout + result.stderr).strip()
        return out[:8000] if len(out) > 8000 else (out or "(no output)")
    except subprocess.TimeoutExpired:
        return f"[TIMEOUT after {timeout}s]"
    except Exception as e:
        return f"[ERROR] {traceback.format_exc()}"

def do_write(filename: str, content: str) -> str:
    path = WORKSPACE / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"✓ Written {path.stat().st_size:,} bytes → {filename}"

def do_read(filename: str, max_lines: int = 200) -> str:
    path = WORKSPACE / filename
    if not path.exists():
        return f"[Not found: {filename}]"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(lines) > max_lines:
        return "\n".join(lines[:max_lines]) + f"\n...[{len(lines)-max_lines} more lines]"
    return "\n".join(lines)

def do_list() -> str:
    rows = []
    for fp in sorted(WORKSPACE.rglob("*")):
        if fp.is_file() and not fp.name.startswith("_"):
            rel = fp.relative_to(WORKSPACE)
            rows.append(f"{rel} ({fp.stat().st_size:,}b)")
    return "\n".join(rows) if rows else "(empty)"

def do_web_search(query: str, num_results: int = 8) -> str:
    try:
        results = exa.search(query, num_results=min(int(num_results), 20), use_autoprompt=True)
        if not results.results:
            return "No results found."
        out = []
        for i, r in enumerate(results.results, 1):
            out.append(f"[{i}] {r.title}\n    {r.url}\n    {r.published_date or 'unknown'}")
        return "\n\n".join(out)
    except Exception as e:
        return f"[Exa error] {e}"

def do_web_fetch(url: str, formats: list = None) -> str:
    try:
        result = firecrawl.scrape_url(url, formats=formats or ["markdown"])
        content = ""
        if hasattr(result, 'markdown'):
            content = result.markdown
        elif isinstance(result, dict):
            content = result.get("markdown", "") or result.get("content", "")
        if len(content) > 12000:
            content = content[:12000] + f"\n\n...[truncated]"
        return content or "[No content]"
    except Exception as e:
        return f"[Firecrawl error] {e}"

def do_search_history(query: str, limit: int = 10) -> str:
    if not HISTORY_F.exists():
        return "No history"
    try:
        data = json.loads(HISTORY_F.read_text())
        matches = []
        for conv in data:
            for msg in conv.get("messages", []):
                if query.lower() in str(msg.get("content", "")).lower():
                    matches.append(msg.get("content", "")[:300])
        return "\n---\n".join(matches[:limit]) if matches else "No matches"
    except:
        return "Error reading history"

def run_tool(name: str, inp: dict) -> str:
    if name == "run_shell":
        return exec_shell(inp["command"], inp.get("timeout", 120))
    elif name == "run_python":
        return exec_python(inp["code"], inp.get("timeout", 60))
    elif name == "write_file":
        return do_write(inp["filename"], inp["content"])
    elif name == "read_file":
        return do_read(inp["filename"], inp.get("max_lines", 200))
    elif name == "list_workspace":
        return do_list()
    elif name == "web_search":
        return do_web_search(inp["query"], inp.get("num_results", 8))
    elif name == "web_fetch":
        return do_web_fetch(inp["url"], inp.get("formats", ["markdown"]))
    elif name == "search_history":
        return do_search_history(inp["query"], inp.get("limit", 10))
    return f"[Unknown tool: {name}]"

def save_conversation(messages: list):
    data = []
    if HISTORY_F.exists():
        try:
            data = json.loads(HISTORY_F.read_text())
        except:
            data = []
    data.append({"date": datetime.now().isoformat()[:16], "messages": messages[-20:]})
    data = data[-200:]
    HISTORY_F.write_text(json.dumps(data, ensure_ascii=False, indent=2))

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    content_bytes = await file.read()
    safe_name = file.filename.replace("..", "").replace("/", "_")
    path = WORKSPACE / safe_name
    path.write_bytes(content_bytes)
    try:
        text = content_bytes.decode("utf-8")
    except:
        text = f"[Binary: {len(content_bytes):,} bytes]"
    return JSONResponse({"filename": safe_name, "size": len(content_bytes), "text": text[:20000]})

@app.get("/api/files")
async def list_files():
    files = []
    for fp in sorted(WORKSPACE.rglob("*")):
        if fp.is_file() and not fp.name.startswith("_"):
            files.append({"name": str(fp.relative_to(WORKSPACE)), "size": fp.stat().st_size})
    return {"files": files}

@app.get("/download/{filename:path}")
async def download(filename: str):
    path = (WORKSPACE / filename).resolve()
    if not str(path).startswith(str(WORKSPACE)):
        return {"error": "Invalid path"}
    if not path.exists():
        return {"error": "Not found"}
    return FileResponse(str(path), filename=path.name)

@app.get("/", response_class=HTMLResponse)
async def root():
    html_path = BASE_DIR / "index.html"
    if html_path.exists():
        return html_path.read_text()
    return "<h1>Rex AI</h1><p>Upload index.html to get started</p>"

@app.post("/chat")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages", [])

    def generate():
        msgs = list(messages)
        while True:
            response = client.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=8096,
                system=SYSTEM,
                tools=TOOLS,
                messages=msgs
            )
            
            for block in response.content:
                if block.type == "text" and block.text:
                    yield f"data: {json.dumps({'type':'text','text':block.text})}\n\n"

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason == "end_turn" or not tool_uses:
                save_conversation(msgs)
                yield "data: [DONE]\n\n"
                break

            msgs.append({"role": "assistant", "content": response.content})
            tool_results = []

            for tu in tool_uses:
                result = run_tool(tu.name, tu.input)
                yield f"data: {json.dumps({'type':'tool','name':tu.name,'result':result})}\n\n"
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": result})

            if tool_results:
                msgs.append({"role": "user", "content": tool_results})

    return StreamingResponse(generate(), media_type="text/event-stream")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

