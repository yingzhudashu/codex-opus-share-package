import os
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import claude_brain_mcp


class ProjectAccessContractTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = os.path.join(self.temp_dir.name, "project")
        os.mkdir(self.project_root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_project_root_requires_absolute_existing_directory(self):
        self.assertIsNotNone(claude_brain_mcp._validate_project_root("")[1])
        self.assertIsNotNone(claude_brain_mcp._validate_project_root("relative/path")[1])
        resolved, error = claude_brain_mcp._validate_project_root(self.project_root)
        self.assertIsNone(error)
        self.assertEqual(resolved, os.path.realpath(self.project_root))

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
            patch.object(claude_brain_mcp.subprocess, "Popen", side_effect=start_process) as popen,
            patch.object(claude_brain_mcp.time, "sleep"),
        ):
            result = claude_brain_mcp._run_claude(
                "plan",
                {"task": "测试短暂错误重试", "project_root": self.project_root},
            )

        self.assertEqual(popen.call_count, 2)
        self.assertEqual(result["structuredContent"]["result"], "完整方案")


if __name__ == "__main__":
    unittest.main()
