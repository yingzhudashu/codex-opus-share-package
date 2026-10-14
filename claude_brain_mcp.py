import json
import os
import subprocess
import sys


def call_tool(name, arguments):
    task = str(arguments.get("task", "")).strip()
    context = str(arguments.get("context", "")).strip()
    effort = str(arguments.get("effort", "high")).strip().lower()
    if not task:
        raise ValueError("task is required")
    if effort not in {"medium", "high", "max"}:
        raise ValueError("effort must be medium, high, or max")
    prompt = f"阶段：{name}\n请求：{task}\n\n上下文：\n{context}\n\n请给出完整、可执行的中文结果，并列出可验证的验收标准。"
    claude_exe = os.path.expandvars(r"%APPDATA%\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe")
    if not os.path.exists(claude_exe):
        raise RuntimeError("Claude Code not found; install it first or set the bridge path for this machine")
    cmd = [claude_exe, "-p", "--model", "claude-opus-5-5", "--effort", effort, "--output-format", "json", prompt]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=150)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "claude failed")
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    payload = json.loads(lines[-1])
    result = payload.get("result", "")
    if not result:
        raise RuntimeError("claude returned empty result")
    return {"content": [{"type": "text", "text": result}], "structuredContent": {"model": payload.get("model", "claude-opus-5-5"), "requested_effort": effort, "stop_reason": payload.get("stop_reason", payload.get("terminal_reason")), "result": result}}


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    request = {}
    try:
        request = json.loads(line)
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            response = {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "claude_brain", "version": "1.0.0"}}}
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            response = {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": n, "description": f"Use Claude Opus 5.5 for {n}.", "inputSchema": {"type": "object", "properties": {"task": {"type": "string"}, "context": {"type": "string"}, "effort": {"type": "string", "enum": ["medium", "high", "max"]}}, "required": ["task"]}} for n in ["plan", "review", "diagnose"]]}}
        elif method == "tools/call":
            params = request.get("params", {})
            response = {"jsonrpc": "2.0", "id": request_id, "result": call_tool(params.get("name", ""), params.get("arguments", {}))}
        else:
            continue
        send(response)
    except Exception as exc:
        send({"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32000, "message": str(exc)}})
