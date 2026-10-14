import os
import tempfile
import unittest
from unittest.mock import patch

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

    def test_command_uses_restricted_read_only_project_tools(self):
        command = claude_brain_mcp._build_claude_command(
            "claude", "plan", "high", "prompt", self.project_root
        )
        self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep")
        self.assertIn("--restricted", command)
        self.assertEqual(command[command.index("--permission-mode") + 1], "plan")
        self.assertEqual(command[command.index("--add-dir") + 1], self.project_root)
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


if __name__ == "__main__":
    unittest.main()
