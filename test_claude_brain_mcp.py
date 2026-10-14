import unittest
from unittest.mock import patch

import claude_brain_mcp


class ContextContractTests(unittest.TestCase):
    def test_context_must_include_project_and_evidence(self):
        self.assertIsNotNone(claude_brain_mcp._validate_context(""))
        self.assertIsNotNone(claude_brain_mcp._validate_context("项目资料"))
        self.assertIsNotNone(
            claude_brain_mcp._validate_context("项目在 D:/repo，请自行检查所有代码和配置。")
        )
        self.assertIsNotNone(
            claude_brain_mcp._validate_context(
                "项目：D:/repo。请检查源码、配置、测试以及所有接口，然后给出详细方案。"
            )
        )

    def test_context_with_file_facts_is_accepted(self):
        context = (
            "项目：D:/repo；分支 main，工作区有 1 个相关修改。\n"
            "已读文件和事实：\n"
            "- src/api.py：路由通过 create_app() 注册，当前使用 v2 接口。\n"
            "- tests/test_api.py：相关用例断言 HTTP 200；本次尚未运行测试。"
        )
        self.assertIsNone(claude_brain_mcp._validate_context(context))

    def test_prompt_contains_evidence_boundaries_and_gap_requirements(self):
        context = "项目：demo。已读文件和事实：- src/app.py 定义应用入口。"
        prompt = claude_brain_mcp._build_prompt("plan", "设计修复", context)
        self.assertIn("<project_context>", prompt)
        self.assertIn(context, prompt)
        self.assertIn("已知事实", prompt)
        self.assertIn("合理假设", prompt)
        self.assertIn("证据缺口", prompt)
        self.assertIn("需要补充证据", prompt)

    def test_missing_context_fails_before_operation_is_queued(self):
        result = claude_brain_mcp.call_tool("plan", {"task": "设计方案", "effort": "high"})
        self.assertEqual(result["structuredContent"]["status"], "failed")
        self.assertEqual(result["structuredContent"]["error_code"], "context_required")
        self.assertIn("已核实事实", result["structuredContent"]["required_context"])
        self.assertEqual(claude_brain_mcp._operations, {})

    def test_valid_context_preserves_async_operation_polling(self):
        context = (
            "项目：D:/repo；当前分支 main。\n"
            "已读文件和事实：\n"
            "- src/api.py：路由通过 create_app() 注册，当前使用 v2 接口。"
        )
        result = {
            "content": [{"type": "text", "text": "方案正文"}],
            "structuredContent": {"requested_model": "claude-opus-5-5", "result": "方案正文"},
        }
        with patch.object(claude_brain_mcp, "_run_claude", return_value=result):
            started = claude_brain_mcp.call_tool(
                "plan", {"task": "制定方案", "context": context, "effort": "max"}
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

    def test_tool_schema_requires_context_on_initial_call(self):
        response = claude_brain_mcp._handle_request({"method": "tools/list", "id": 1})
        tool = response["result"]["tools"][0]
        self.assertIn("context", tool["inputSchema"]["oneOf"][0]["required"])
        self.assertGreaterEqual(
            tool["inputSchema"]["properties"]["context"]["minLength"],
            claude_brain_mcp.MIN_CONTEXT_CHARACTERS,
        )


if __name__ == "__main__":
    unittest.main()
