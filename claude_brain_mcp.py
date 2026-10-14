import json
import os
import subprocess
import sys
import tempfile
import time


REQUEST_TIMEOUT_SECONDS = 90
TOTAL_TIMEOUT_SECONDS = 150
MAX_RETRIES = 1


class ClaudeCallError(RuntimeError):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def _terminate_process_tree(proc):
    """Stop Claude Code and descendants so a timed-out request cannot linger."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except subprocess.TimeoutExpired:
            pass
    else:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _json_payload(stdout):
    for line in reversed(stdout.splitlines()):
        if line.lstrip().startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
    return None


def _retryable_failure(payload, detail):
    status = payload.get("api_error_status") if payload else None
    message = (detail or "").lower()
    permanent_markers = (
        "authentication",
        "unauthorized",
        "invalid api key",
        "permission denied",
        "insufficient balance",
        "billing",
        "credit balance",
        "quota exceeded",
    )
    if any(marker in message for marker in permanent_markers):
        return False
    if status in {408, 429, 500, 502, 503, 504, "408", "429", "500", "502", "503", "504"}:
        return True
    transient_markers = (
        "timed out",
        "timeout",
        "temporarily unavailable",
        "overloaded",
        "rate limit",
        "connection reset",
        "connection refused",
        "network error",
        "server error",
    )
    return any(marker in message for marker in transient_markers)


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
    # Brain calls need text reasoning only. Isolate them from project files, hooks,
    # plugins, LSP and session persistence so a large worktree cannot block a call.
    cmd = [
        claude_exe,
        "-p",
        "--bare",
        "--no-session-persistence",
        "--tools",
        "",
        "--strict-mcp-config",
        "--setting-sources",
        "user",
        "--model",
        "claude-opus-5-5",
        "--effort",
        effort,
        "--output-format",
        "json",
        prompt,
    ]
    deadline = time.monotonic() + TOTAL_TIMEOUT_SECONDS
    payload = None
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            break
        timeout = min(REQUEST_TIMEOUT_SECONDS, remaining)
        with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(
            mode="w+b"
        ) as stderr_file:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
            )
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_tree(proc)
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read().decode("utf-8", errors="replace")
            stderr = stderr_file.read().decode("utf-8", errors="replace")

        if timed_out:
            last_error = ClaudeCallError(
                f"Claude request timed out after {int(timeout)} seconds",
                retryable=True,
            )
        else:
            payload = _json_payload(stdout)
            if proc.returncode != 0 or (payload and payload.get("is_error")):
                detail = " ".join((stderr or "", stdout or ""))
                status = payload.get("api_error_status") if payload else None
                retryable = _retryable_failure(payload, detail)
                last_error = ClaudeCallError(
                    f"Claude request failed (API status {status})"
                    if status
                    else f"Claude Code exited with status {proc.returncode}",
                    retryable=retryable,
                )
            elif payload is None:
                raise RuntimeError("Claude returned invalid JSON")
            else:
                break

        if attempt >= MAX_RETRIES or not last_error.retryable:
            raise last_error
        if deadline - time.monotonic() <= 2:
            raise last_error
        time.sleep(1)
    else:
        raise last_error or RuntimeError("Claude request exceeded its total time limit")
    if last_error and payload is None:
        raise last_error
    result = payload.get("result", "")
    if not result:
        raise RuntimeError("claude returned empty result")
    model_usage = payload.get("modelUsage") or {}
    declared_model = payload.get("model") or next(iter(model_usage), None)
    return {
        "content": [{"type": "text", "text": result}],
        "structuredContent": {
            "model": declared_model or "model not reported",
            "requested_effort": effort,
            "stop_reason": payload.get("stop_reason", payload.get("terminal_reason")),
            "duration_ms": payload.get("duration_ms"),
            "result": result,
        },
    }


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
