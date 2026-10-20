import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import Future
from unittest.mock import Mock, patch

import claude_brain_mcp


class ProjectAccessContractTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = os.path.join(self.temp_dir.name, "project")
        os.mkdir(self.project_root)
        with claude_brain_mcp._operations_lock:
            claude_brain_mcp._operations.clear()

    def tearDown(self):
        with claude_brain_mcp._operations_lock:
            claude_brain_mcp._operations.clear()
        self.temp_dir.cleanup()

    @staticmethod
    def _operation(future, created_at, tool="plan"):
        return {
            "future": future,
            "created_at": created_at,
            "tool": tool,
            "effort": "medium",
        }

    def test_project_root_requires_absolute_existing_directory(self):
        self.assertIsNotNone(claude_brain_mcp._validate_project_root("")[1])
        self.assertIsNotNone(claude_brain_mcp._validate_project_root("relative/path")[1])
        resolved, error = claude_brain_mcp._validate_project_root(self.project_root)
        self.assertIsNone(error)
        self.assertEqual(resolved, os.path.realpath(self.project_root))

    def test_project_root_length_is_bounded(self):
        too_long = "X:/" + ("a" * claude_brain_mcp.MAX_PROJECT_ROOT_LENGTH)
        resolved, error = claude_brain_mcp._validate_project_root(too_long)
        self.assertIsNone(resolved)
        self.assertIn("路径过长", error)

    def test_claude_executable_uses_configured_absolute_path(self):
        configured = os.path.join(self.temp_dir.name, "claude.exe")
        with (
            patch.dict(os.environ, {claude_brain_mcp.CLAUDE_CODE_PATH_ENV: configured}),
            patch.object(claude_brain_mcp.os.path, "isfile", return_value=True),
            patch.object(claude_brain_mcp.shutil, "which", return_value=None),
        ):
            self.assertEqual(
                claude_brain_mcp._resolve_claude_executable(),
                os.path.realpath(configured),
            )

    def test_missing_configured_executable_does_not_fall_back_to_path(self):
        configured = os.path.join(self.temp_dir.name, "missing-claude.exe")
        with (
            patch.dict(os.environ, {claude_brain_mcp.CLAUDE_CODE_PATH_ENV: configured}),
            patch.object(claude_brain_mcp.os.path, "isfile", return_value=False),
            patch.object(claude_brain_mcp.shutil, "which", return_value="/usr/bin/claude"),
        ):
            with self.assertRaisesRegex(RuntimeError, "configured CLAUDE_CODE_PATH executable was not found"):
                claude_brain_mcp._resolve_claude_executable()

    def test_claude_executable_rejects_relative_override(self):
        with patch.dict(os.environ, {claude_brain_mcp.CLAUDE_CODE_PATH_ENV: "claude.exe"}):
            with self.assertRaisesRegex(RuntimeError, "must be an absolute"):
                claude_brain_mcp._resolve_claude_executable()

    def test_claude_executable_rejects_windows_command_wrapper(self):
        wrapper = os.path.join(self.temp_dir.name, "claude.cmd")
        with (
            patch.object(claude_brain_mcp.os, "name", "nt"),
            patch.dict(os.environ, {claude_brain_mcp.CLAUDE_CODE_PATH_ENV: wrapper}),
            patch.object(claude_brain_mcp.os.path, "isfile", return_value=True),
            patch.object(claude_brain_mcp.shutil, "which", return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "native .exe or .com"):
                claude_brain_mcp._resolve_claude_executable()

    def test_claude_executable_falls_back_to_path(self):
        executable = os.path.join(self.temp_dir.name, "claude")
        with (
            patch.object(claude_brain_mcp.os, "name", "posix"),
            patch.dict(os.environ, {}, clear=True),
            patch.object(claude_brain_mcp.shutil, "which", return_value=executable),
            patch.object(claude_brain_mcp.os.path, "isfile", return_value=True),
        ):
            self.assertEqual(claude_brain_mcp._resolve_claude_executable(), os.path.realpath(executable))

    def test_oversized_task_and_context_fail_before_queueing(self):
        with patch.object(claude_brain_mcp._executor, "submit") as submit:
            with self.assertRaisesRegex(ValueError, "task must be at most"):
                claude_brain_mcp.call_tool(
                    "plan",
                    {
                        "task": "x" * (claude_brain_mcp.MAX_TASK_LENGTH + 1),
                        "project_root": self.project_root,
                    },
                )
            with self.assertRaisesRegex(ValueError, "context must be at most"):
                claude_brain_mcp.call_tool(
                    "plan",
                    {
                        "task": "short task",
                        "context": "x" * (claude_brain_mcp.MAX_CONTEXT_LENGTH + 1),
                        "project_root": self.project_root,
                    },
                )
        submit.assert_not_called()

    def test_prompt_contains_evidence_boundaries_and_gap_requirements(self):
        context = "复现步骤：启动 web 服务后请求 /health 返回 500。"
        prompt = claude_brain_mcp._build_prompt("plan", "设计修复", context, self.project_root)
        self.assertIn(self.project_root, prompt)
        self.assertIn("主动使用 Read、Glob、Grep", prompt)
        self.assertIn("<caller_context>", prompt)
        self.assertIn(context, prompt)
        self.assertIn("已知事实", prompt)
        self.assertIn("合理假设", prompt)
        self.assertIn("证据缺口", prompt)
        self.assertIn("需要补充证据", prompt)
        self.assertIn("不得修改文件、运行命令", prompt)

    def test_command_uses_safe_mode_read_only_project_tools(self):
        command = claude_brain_mcp._build_claude_command(
            "claude", "plan", "high", "prompt", self.project_root
        )
        self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep")
        self.assertIn("--safe-mode", command)
        self.assertNotIn("--restricted", command)
        self.assertEqual(command[command.index("--permission-mode") + 1], "plan")
        self.assertEqual(command[command.index("--add-dir") + 1], self.project_root)
        self.assertNotIn("--bare", command)
        self.assertNotIn("Edit", command[command.index("--tools") + 1])
        self.assertNotIn("Bash", command[command.index("--tools") + 1])
        self.assertNotIn("Write", command[command.index("--tools") + 1])
        denied = command[command.index("--disallowedTools") + 1 : command.index("--strict-mcp-config")]
        for tool in ("Read", "Grep"):
            self.assertIn(f"{tool}(**/.env)", denied)
            self.assertIn(f"{tool}(**/*.pem)", denied)
            self.assertIn(f"{tool}(**/*.pfx)", denied)
            self.assertIn(f"{tool}(**/*credential*)", denied)
            self.assertIn(f"{tool}(**/.ssh/**)", denied)

    def test_missing_project_root_fails_before_operation_is_queued(self):
        result = claude_brain_mcp.call_tool("plan", {"task": "设计方案", "effort": "high"})
        self.assertEqual(result["structuredContent"]["status"], "failed")
        self.assertEqual(result["structuredContent"]["error_code"], "project_root_required")
        self.assertIn("项目仓库绝对路径", result["structuredContent"]["required_project_root"])
        self.assertEqual(claude_brain_mcp._operations, {})

    def test_nonexistent_project_root_fails_before_operation_is_queued(self):
        missing_path = os.path.join(self.temp_dir.name, "missing")
        result = claude_brain_mcp.call_tool(
            "plan",
            {"task": "设计方案", "project_root": missing_path, "effort": "high"},
        )
        self.assertEqual(result["structuredContent"]["status"], "failed")
        self.assertEqual(result["structuredContent"]["error_code"], "project_root_required")
        self.assertEqual(claude_brain_mcp._operations, {})

    def test_valid_context_preserves_async_operation_polling(self):
        result = {
            "content": [{"type": "text", "text": "方案正文"}],
            "structuredContent": {"requested_model": "claude-opus-5-5", "result": "方案正文"},
        }
        with patch.object(claude_brain_mcp, "_run_claude", return_value=result):
            started = claude_brain_mcp.call_tool(
                "plan",
                {
                    "task": "制定方案",
                    "project_root": self.project_root,
                    "context": "检查 src/api.py 中的 v2 路由。",
                    "effort": "max",
                },
            )
            self.assertEqual(started["structuredContent"]["status"], "running")
            operation_id = started["structuredContent"]["operation_id"]
            completed = claude_brain_mcp.call_tool(
                "plan", {"operation_id": operation_id, "wait_seconds": 1}
            )
            self.assertEqual(completed["structuredContent"]["status"], "completed")
            self.assertEqual(completed["structuredContent"]["result"], "方案正文")
            with claude_brain_mcp._operations_lock:
                claude_brain_mcp._operations.pop(operation_id, None)

    def test_tool_schema_requires_project_root_on_initial_call(self):
        response = claude_brain_mcp._handle_request({"method": "tools/list", "id": 1})
        tool = response["result"]["tools"][0]
        self.assertIn("project_root", tool["inputSchema"]["oneOf"][0]["required"])
        self.assertNotIn("context", tool["inputSchema"]["oneOf"][0]["required"])
        self.assertEqual(
            tool["inputSchema"]["properties"]["task"]["maxLength"],
            claude_brain_mcp.MAX_TASK_LENGTH,
        )
        self.assertEqual(
            tool["inputSchema"]["properties"]["context"]["maxLength"],
            claude_brain_mcp.MAX_CONTEXT_LENGTH,
        )
        self.assertEqual(
            tool["inputSchema"]["properties"]["project_root"]["maxLength"],
            claude_brain_mcp.MAX_PROJECT_ROOT_LENGTH,
        )

    def test_poll_schema_does_not_require_project_root(self):
        response = claude_brain_mcp._handle_request({"method": "tools/list", "id": 1})
        tool = response["result"]["tools"][0]
        poll_variant = tool["inputSchema"]["oneOf"][1]
        self.assertIn("operation_id", poll_variant["required"])
        self.assertIn("project_root", poll_variant["not"]["required"])

    def test_poll_rejects_repeated_initial_request_fields(self):
        with self.assertRaisesRegex(ValueError, "must not repeat"):
            claude_brain_mcp.call_tool(
                "plan",
                {
                    "operation_id": "unknown-operation",
                    "project_root": self.project_root,
                    "wait_seconds": 0,
                },
            )

    def test_poll_rejects_invalid_wait_values(self):
        for value in (True, False, "1", float("nan"), float("inf"), -0.1, 20.1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "wait_seconds must be a number"):
                    claude_brain_mcp.call_tool("plan", {"operation_id": "unused", "wait_seconds": value})

    def test_poll_rejects_operation_from_another_tool(self):
        future = Future()
        future.set_result({"content": [], "structuredContent": {"result": "result"}})
        with claude_brain_mcp._operations_lock:
            claude_brain_mcp._operations["operation-for-review"] = self._operation(
                future, claude_brain_mcp.time.monotonic(), tool="review"
            )
        with self.assertRaisesRegex(ValueError, "different Claude tool"):
            claude_brain_mcp.call_tool("plan", {"operation_id": "operation-for-review", "wait_seconds": 0})

    def test_concurrent_limit_rejects_fifth_inflight_call(self):
        pending = [Mock(done=Mock(return_value=False)) for _ in range(claude_brain_mcp.MAX_CONCURRENT_CALLS)]
        with claude_brain_mcp._operations_lock:
            for index, future in enumerate(pending):
                claude_brain_mcp._operations[f"pending-{index}"] = self._operation(
                    future, claude_brain_mcp.time.monotonic()
                )
        with patch.object(claude_brain_mcp._executor, "submit") as submit:
            with self.assertRaisesRegex(RuntimeError, "concurrent request limit"):
                claude_brain_mcp.call_tool(
                    "plan", {"task": "排队限制测试", "project_root": self.project_root}
                )
        submit.assert_not_called()

    def test_operation_ttl_removes_only_expired_completed_records(self):
        expired = Future()
        expired.set_result({})
        fresh = Future()
        fresh.set_result({})
        with patch.object(claude_brain_mcp, "OPERATION_TTL_SECONDS", 10):
            with claude_brain_mcp._operations_lock:
                claude_brain_mcp._operations["expired"] = self._operation(expired, 0)
                claude_brain_mcp._operations["fresh"] = self._operation(fresh, 5)
                claude_brain_mcp._prune_operations(11)
                self.assertNotIn("expired", claude_brain_mcp._operations)
                self.assertIn("fresh", claude_brain_mcp._operations)

    def test_retention_cap_prunes_oldest_completed_but_keeps_running(self):
        completed = []
        for _ in range(3):
            future = Future()
            future.set_result({})
            completed.append(future)
        running = Mock(done=Mock(return_value=False))
        with patch.object(claude_brain_mcp, "MAX_RETAINED_OPERATIONS", 2):
            with claude_brain_mcp._operations_lock:
                claude_brain_mcp._operations["oldest"] = self._operation(completed[0], 1)
                claude_brain_mcp._operations["middle"] = self._operation(completed[1], 2)
                claude_brain_mcp._operations["newest"] = self._operation(completed[2], 3)
                claude_brain_mcp._operations["running"] = self._operation(running, 0)
                claude_brain_mcp._prune_operations(4)
                self.assertNotIn("oldest", claude_brain_mcp._operations)
                self.assertNotIn("middle", claude_brain_mcp._operations)
                self.assertIn("newest", claude_brain_mcp._operations)
                self.assertIn("running", claude_brain_mcp._operations)

    def test_stdio_emits_utf8_json_rpc_without_non_protocol_stdout(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        completed = subprocess.run(
            [sys.executable, os.path.abspath(claude_brain_mcp.__file__)],
            input="\n".join(json.dumps(item, ensure_ascii=False) for item in requests) + "\n",
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=True,
        )
        frames = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([frame["id"] for frame in frames], [1, 2])
        project_description = frames[1]["result"]["tools"][0]["inputSchema"]["properties"]["project_root"]["description"]
        self.assertIn("项目仓库绝对路径", project_description)
        self.assertEqual(completed.stderr, "")

    def test_failure_message_classifies_oauth_failure_without_raw_output(self):
        message = claude_brain_mcp._safe_failure_message(
            None,
            "Not logged in · Please run /login\nAuthorization: Bearer do-not-print-this",
            1,
        )
        self.assertIn("无法使用当前登录凭据", message)
        self.assertNotIn("do-not-print-this", message)

    def test_failure_message_keeps_unclassified_exit_code_useful(self):
        message = claude_brain_mcp._safe_failure_message(None, "opaque failure detail", 7)
        self.assertIn("退出码 7", message)
        self.assertNotIn("opaque failure detail", message)

    def test_local_process_timeout_does_not_retry_ambiguous_request(self):
        process = Mock()
        process.wait.side_effect = subprocess.TimeoutExpired("claude", 1)
        process.returncode = -1

        with (
            patch.object(claude_brain_mcp.os.path, "exists", return_value=True),
            patch.object(claude_brain_mcp, "_resolve_claude_executable", return_value="claude.exe"),
            patch.object(claude_brain_mcp.subprocess, "Popen", return_value=process) as popen,
            patch.object(claude_brain_mcp, "_terminate_process_tree"),
            patch.object(claude_brain_mcp, "REQUEST_TIMEOUT_SECONDS", 1),
            patch.object(claude_brain_mcp, "TOTAL_TIMEOUT_SECONDS", 10),
            patch.object(claude_brain_mcp, "PROCESS_TERMINATION_RESERVE_SECONDS", 1),
        ):
            with self.assertRaisesRegex(
                claude_brain_mcp.ClaudeCallError,
                "未自动重试",
            ):
                claude_brain_mcp._run_claude(
                    "plan",
                    {"task": "测试超时", "project_root": self.project_root},
                )

        popen.assert_called_once()
        self.assertFalse(popen.call_args.kwargs["start_new_session"])

    def test_posix_process_uses_separate_session_for_tree_cleanup(self):
        process = Mock()
        process.wait.return_value = 0
        process.returncode = 0
        responses = ['{"result":"完整结果","model":"claude-opus-5-5"}\n']

        def start_process(*args, **kwargs):
            kwargs["stdout"].write(responses.pop(0).encode("utf-8"))
            return process

        with (
            patch.object(claude_brain_mcp.os, "name", "posix"),
            patch.object(claude_brain_mcp, "_resolve_claude_executable", return_value="/usr/bin/claude"),
            patch.object(claude_brain_mcp.subprocess, "Popen", side_effect=start_process) as popen,
        ):
            result = claude_brain_mcp._run_claude(
                "plan", {"task": "POSIX session test", "project_root": self.project_root}
            )

        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(result["structuredContent"]["result"], "完整结果")

    def test_bridge_timeout_budgets_allow_one_long_request(self):
        self.assertEqual(claude_brain_mcp.REQUEST_TIMEOUT_SECONDS, 600)
        self.assertEqual(claude_brain_mcp.TOTAL_TIMEOUT_SECONDS, 900)
        self.assertGreater(
            claude_brain_mcp.TOTAL_TIMEOUT_SECONDS,
            claude_brain_mcp.REQUEST_TIMEOUT_SECONDS
            + claude_brain_mcp.PROCESS_TERMINATION_RESERVE_SECONDS,
        )

    def test_explicit_transient_service_error_retries_once(self):
        responses = [
            '{"is_error":true,"api_error_status":503}\n',
            '{"result":"完整方案","model":"claude-opus-5-5","stop_reason":"end_turn"}\n',
        ]

        def start_process(*args, **kwargs):
            kwargs["stdout"].write(responses.pop(0).encode("utf-8"))
            process = Mock()
            process.wait.return_value = 0
            process.returncode = 0
            return process

        with (
            patch.object(claude_brain_mcp.os.path, "exists", return_value=True),
            patch.object(claude_brain_mcp, "_resolve_claude_executable", return_value="claude.exe"),
            patch.object(claude_brain_mcp.subprocess, "Popen", side_effect=start_process) as popen,
            patch.object(claude_brain_mcp.time, "sleep"),
        ):
            result = claude_brain_mcp._run_claude(
                "plan",
                {"task": "测试短暂错误重试", "project_root": self.project_root},
            )

        self.assertEqual(popen.call_count, 2)
        self.assertEqual(result["structuredContent"]["result"], "完整方案")

    def test_explicit_max_tokens_stop_reason_is_rejected_as_truncated(self):
        process = Mock()
        process.wait.return_value = 0
        process.returncode = 0

        def start_process(*args, **kwargs):
            kwargs["stdout"].write(
                b'{"result":"partial plan","model":"claude-opus-5-5",'
                b'"stop_reason":"max_tokens"}\n'
            )
            return process

        with (
            patch.object(claude_brain_mcp.os.path, "exists", return_value=True),
            patch.object(claude_brain_mcp, "_resolve_claude_executable", return_value="claude.exe"),
            patch.object(claude_brain_mcp.subprocess, "Popen", side_effect=start_process),
        ):
            with self.assertRaisesRegex(RuntimeError, "truncated at the output token limit"):
                claude_brain_mcp._run_claude(
                    "plan", {"task": "截断结果测试", "project_root": self.project_root}
                )

    def test_explicit_truncated_flags_are_rejected(self):
        for field in ("truncated", "is_truncated"):
            with self.subTest(field=field):
                process = Mock()
                process.wait.return_value = 0
                process.returncode = 0

                def start_process(*args, **kwargs):
                    payload = {"result": "partial plan", "model": "claude-opus-5-5", field: True}
                    kwargs["stdout"].write((json.dumps(payload) + "\n").encode("utf-8"))
                    return process

                with (
                    patch.object(claude_brain_mcp.os.path, "exists", return_value=True),
                    patch.object(claude_brain_mcp, "_resolve_claude_executable", return_value="claude.exe"),
                    patch.object(claude_brain_mcp.subprocess, "Popen", side_effect=start_process),
                ):
                    with self.assertRaisesRegex(RuntimeError, "response was marked as truncated"):
                        claude_brain_mcp._run_claude(
                            "plan", {"task": "截断标志测试", "project_root": self.project_root}
                        )


if __name__ == "__main__":
    unittest.main()
