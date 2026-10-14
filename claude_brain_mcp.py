import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout


REQUEST_TIMEOUT_SECONDS = 90
TOTAL_TIMEOUT_SECONDS = 150
MAX_RETRIES = 1
MAX_CONCURRENT_CALLS = 4
MAX_POLL_SECONDS = 20
PROCESS_TERMINATION_RESERVE_SECONDS = 15
OPERATION_TTL_SECONDS = 3600
MAX_RETAINED_OPERATIONS = 64

_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CALLS)
_operations = {}
_operations_lock = threading.Lock()


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
        except (OSError, subprocess.TimeoutExpired):
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
    if status in {
        408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529,
        "408", "429", "500", "502", "503", "504", "520", "521", "522", "523", "524", "529",
    }:
        return True
    transient_markers = (
        "timed out",
        "timeout",
        "temporarily unavailable",
        "overloaded",
        "overload",
        "rate limit",
        "connection reset",
        "connection refused",
        "network error",
        "server error",
    )
    return any(marker in message for marker in transient_markers)


def _run_claude(name, arguments):
    task = str(arguments.get("task", "")).strip()
    context = str(arguments.get("context", "")).strip()
    effort = str(arguments.get("effort", "medium")).strip().lower()
    if not task:
        raise ValueError("task is required")
    if effort not in {"medium", "high", "max"}:
        raise ValueError("effort must be medium, high, or max")
    prompt = f"阶段：{name}\n请求：{task}\n\n上下文：\n{context}\n\n请给出完整、可执行的中文结果，并列出可验证的验收标准。"
    claude_exe = os.path.expandvars(r"%APPDATA%\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe")
    if not os.path.exists(claude_exe):
        raise RuntimeError("Claude Code executable is not installed at the configured location")
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
        "--disable-slash-commands",
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
        if remaining <= PROCESS_TERMINATION_RESERVE_SECONDS + 1:
            raise last_error or RuntimeError("Claude request exceeded its total time limit")
        with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(
            mode="w+b"
        ) as stderr_file:
            proc = subprocess.Popen(
                cmd,
                cwd=tempfile.gettempdir(),
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
            )
            remaining = deadline - time.monotonic()
            timeout = min(
                REQUEST_TIMEOUT_SECONDS,
                max(0, remaining - PROCESS_TERMINATION_RESERVE_SECONDS),
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
                if status:
                    failure = f"Claude request failed (API status {status})"
                elif retryable:
                    failure = "Claude request failed with a transient network or server error"
                elif any(marker in detail.lower() for marker in ("authentication", "unauthorized", "invalid api key")):
                    failure = "Claude authentication failed"
                elif any(marker in detail.lower() for marker in ("permission denied", "forbidden")):
                    failure = "Claude request was rejected by permission policy"
                elif any(marker in detail.lower() for marker in ("insufficient balance", "credit balance", "quota exceeded", "billing")):
                    failure = "Claude provider reported a billing or quota limit"
                else:
                    failure = f"Claude Code exited with status {proc.returncode}"
                last_error = ClaudeCallError(
                    failure,
                    retryable=retryable,
                )
            elif payload is None:
                raise RuntimeError("Claude returned invalid JSON")
            else:
                break

        if attempt >= MAX_RETRIES or not last_error.retryable:
            raise last_error
        if deadline - time.monotonic() <= PROCESS_TERMINATION_RESERVE_SECONDS + 1:
            raise last_error
        time.sleep(1)
    else:
        raise last_error or RuntimeError("Claude request exceeded its total time limit")
    if last_error and payload is None:
        raise last_error
    result = payload.get("result", "")
    if not isinstance(result, str) or not result.strip():
        raise RuntimeError("claude returned empty result")
    model_usage = payload.get("modelUsage") or {}
    declared_model = payload.get("model") or next(iter(model_usage), None)
    return {
        "content": [{"type": "text", "text": result}],
        "structuredContent": {
            "requested_model": "claude-opus-5-5",
            "model": declared_model or "model not reported",
            "requested_effort": effort,
            "stop_reason": payload.get("stop_reason", payload.get("terminal_reason")),
            "duration_ms": payload.get("duration_ms"),
            "result": result,
        },
    }


def _prune_operations(now):
    expired = [
        operation_id
        for operation_id, operation in _operations.items()
        if operation["future"].done()
        and now - operation["created_at"] > OPERATION_TTL_SECONDS
    ]
    for operation_id in expired:
        del _operations[operation_id]
    completed = sorted(
        (
            (operation_id, operation)
            for operation_id, operation in _operations.items()
            if operation["future"].done()
        ),
        key=lambda pair: pair[1]["created_at"],
    )
    for operation_id, _ in completed[: max(0, len(_operations) - MAX_RETAINED_OPERATIONS)]:
        del _operations[operation_id]


def call_tool(name, arguments):
    operation_id = str(arguments.get("operation_id", "")).strip()
    if operation_id and str(arguments.get("task", "")).strip():
        raise ValueError("polling must not repeat the original task")
    wait_seconds = arguments.get("wait_seconds", 0)
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)):
        raise ValueError("wait_seconds must be a number between 0 and 20")
    try:
        wait_seconds = float(wait_seconds)
    except (OverflowError, ValueError):
        raise ValueError("wait_seconds must be a number between 0 and 20") from None
    if not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= MAX_POLL_SECONDS:
        raise ValueError("wait_seconds must be a number between 0 and 20")

    if operation_id:
        with _operations_lock:
            operation = _operations.get(operation_id)
            if (
                operation is not None
                and operation["future"].done()
                and time.monotonic() - operation["created_at"] > OPERATION_TTL_SECONDS
            ):
                del _operations[operation_id]
                operation = None
        if operation is None:
            raise ValueError("operation_id is unknown or expired")
        if operation["tool"] != name:
            raise ValueError("operation_id belongs to a different Claude tool")
        future = operation["future"]
        try:
            result = future.result(timeout=wait_seconds)
        except FutureTimeout:
            return {
                "content": [{
                    "type": "text",
                    "text": f"Claude 仍在处理。请使用同一 operation_id 继续轮询；不要重新提交原任务。",
                }],
                "structuredContent": {
                    "status": "running",
                    "operation_id": operation_id,
                    "requested_model": "claude-opus-5-5",
                    "requested_effort": operation["effort"],
                },
            }
        except Exception as exc:
            return {
                "content": [{"type": "text", "text": f"Claude 调用失败：{exc}"}],
                "structuredContent": {
                    "status": "failed",
                    "operation_id": operation_id,
                    "requested_model": "claude-opus-5-5",
                    "requested_effort": operation["effort"],
                    "duration_ms": int((time.monotonic() - operation["created_at"]) * 1000),
                },
            }
        final_result = dict(result)
        final_result["structuredContent"] = {
            **result["structuredContent"],
            "status": "completed",
            "operation_id": operation_id,
        }
        return final_result

    task = str(arguments.get("task", "")).strip()
    if not task:
        raise ValueError("task is required")
    effort = str(arguments.get("effort", "medium")).strip().lower()
    if effort not in {"medium", "high", "max"}:
        raise ValueError("effort must be medium, high, or max")

    with _operations_lock:
        _prune_operations(time.monotonic())
        active = sum(not item["future"].done() for item in _operations.values())
        if active >= MAX_CONCURRENT_CALLS:
            raise RuntimeError("Claude bridge is at its concurrent request limit; retry shortly")
        operation_id = str(uuid.uuid4())
        future = _executor.submit(_run_claude, name, dict(arguments))
        _operations[operation_id] = {
            "future": future,
            "tool": name,
            "effort": effort,
            "created_at": time.monotonic(),
        }

    return {
        "content": [{
            "type": "text",
            "text": "Claude 请求已启动。请使用返回的 operation_id 轮询结果，不要重复提交原任务。",
        }],
        "structuredContent": {
            "status": "running",
            "operation_id": operation_id,
            "requested_model": "claude-opus-5-5",
            "requested_effort": effort,
        },
    }


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle_request(request):
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "claude_brain", "version": "1.1.0"}}}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        common_schema = {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "context": {"type": "string"},
                "effort": {
                    "type": "string",
                    "enum": ["medium", "high", "max"],
                    "default": "medium",
                },
                "operation_id": {"type": "string"},
                "wait_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": MAX_POLL_SECONDS,
                },
            },
            "oneOf": [
                {"required": ["task"], "not": {"required": ["operation_id"]}},
                {"required": ["operation_id"], "not": {"required": ["task"]}},
            ],
        }
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": n, "description": f"Use Claude Opus 5.5 for {n}. First call with task/context/effort returns an operation_id immediately. Then call the same tool with operation_id and wait_seconds (0-20) until structuredContent.status is completed or failed. Never resubmit a running operation.", "inputSchema": common_schema} for n in ["plan", "review", "diagnose"]]}}
    if method == "tools/call":
        params = request.get("params", {})
        name = params.get("name", "")
        if name not in {"plan", "review", "diagnose"}:
            raise ValueError("unknown Claude brain tool")
        return {"jsonrpc": "2.0", "id": request_id, "result": call_tool(name, params.get("arguments", {}))}
    return None


def main():
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            response = _handle_request(request)
            if response is not None:
                send(response)
        except Exception as exc:
            send({"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32000, "message": str(exc)}})


if __name__ == "__main__":
    main()
