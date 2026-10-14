# 全局提示词脱敏分享包 v2

包含脱敏全局规则、新客户端接入提示词，以及“自己填 API 地址和 Key”的配置模板。保留原有执行方式、生产授权、最小验证、项目记忆、停止条件和 Claude 规划复核要求。

## 快速安装

Windows 新电脑准备好 Codex、Python 3 和 Claude Code 后，在本目录 PowerShell 执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

安装器会自动定位当前用户的 `CODEX_HOME`，备份已有 `AGENTS.md` 和 `config.toml`，安装 `claude_brain` 桥接并注册为全局 Codex MCP。它不会复制 API Key，也不会覆盖现有模型供应商配置；Claude Code 的 API 地址和凭据需要在新电脑单独配置。安装结束后重启 Codex 或新建会话。

Claude 的 plan/review/diagnose 工具使用异步请求：首次调用立即返回 `operation_id`，之后用同一工具轮询到 `completed` 或 `failed`。轮询最多等待 20 秒；不要重复提交正在运行的原始请求。这样可避开 Codex MCP 单次调用约 60 秒的等待限制，同时保留桥接自身的 150 秒总请求预算。

桥接会强制 MCP stdio 使用 UTF-8，避免 Windows 系统代码页（如 GBK）导致中文回包无法被 Codex 解码。

首次调用必须同时传入 `task`、`context` 和 `effort`。调用前由 Codex 读取当前项目中与任务直接相关的规则、上下文入口、源码/配置、测试和必要的 Git 差异，再把项目和 `- 文件路径：已核实事实` 条目整理进 `context`；复核还需包含最终差异、实际测试结果和未验证项。桥接不会自动扫描工作区，且会拒绝空、过短、泛化或缺少路径事实条目的上下文。Claude 只能依据已传材料给建议，应标注事实、假设及证据缺口；这项格式检查本身不代表材料内容已被独立验证。

卸载本包注册的 MCP：

```powershell
codex mcp remove claude_brain
```

## 对方怎么用

1. 解压后，把本包交给新客户端 AI，复制《新客户端接入提示词》中的整段任务。
2. 填写自己的 API 地址；API Key 只在 AI 准备好的本机私密入口或私有 Key 文件填写，不能填进聊天或共享模板。模型已预设为 `claude-opus-5-5`。
3. AI 安装规则、连接已有工具或按授权创建最小本地桥接，并做离线验证。需要真实 high/max 验收时，再明确允许文中最多两次的小样本调用；它们可能计费。

本包包含 Windows 安装器和不保存 Key 的本地 MCP 桥接，不包含任何真实 Key。接收方仍需在新电脑安装 Codex、Python 3、Claude Code，并用自己的凭据完成 Claude Code 登录；不要把填好配置与实际 API 验证混为一谈。

## 包内文件

| 文件 | 作用 |
| --- | --- |
| `AGENTS.md` | 十一章脱敏全局规则，已增加自备 Opus 配置要求 |
| `新客户端接入提示词.md` | 交给 AI 的安装、工具接入、私密配置和验收任务 |
| `本地配置.example.md` | 两个必填项、可选参数、私有存放位置说明 |
| `opus-config.example.json` | API 地址、模型、协议、档位、超时与验收开关的模板 |
| `opus-key.example.env` | Key 的空白模板，只能在本机另存私有副本填写 |
| `README.md` | 本说明 |
| `install.ps1` | Windows 一键安装器，自动备份、安装桥接和注册 MCP |
| `claude_brain_mcp.py` | 不保存 Key 的 Claude Opus 5.5 MCP 桥接 |

`.example` 文件永远保持占位符。填写后的 `opus-config.json`、`opus-key.env` 应在分享目录、项目版本库、同步目录之外，由本地凭据存储或受限权限保护，不能加回 ZIP。配置模板不是客户端原生配置；安装器会注册包内桥接，但不会自动加载或复制 `.env`。

## 接口约定

- 模型标识：`claude-opus-5-5`。
- 协议：Anthropic Messages，通常为 `POST /v1/messages`。
- 官方基础地址：`https://api.anthropic.com`；第三方使用自己的 HTTPS 地址，并核实协议及能力。
- 档位参数：`output_config.effort`，一般任务 `high`，复杂与审美任务 `max`；本工作流的默认档对应 `medium`。
- 项目上下文：初次调用要求提供基于已读项目资料的 `context`；桥接不自行读取或上传仓库文件。
- 模板中保留 90 秒单请求、150 秒总上限及最多一次短暂故障重试；受限的首次真实验收另外采用两次请求且不重试。

真实请求必须核对模型字段、请求参数、完整非空正文与停止原因；网关返回模型名只是其声明，不等于独立上游身份认证。不支持这些要求的服务不能用降档或改协议假装通过。

## 脱敏与原规则保护

删除原作者的服务器地址、账号、别名、私有路径、脚本位置、恢复记录及连接状态，不附带认证文件、源码、客户数据或运行日志。原作者的实际全局配置未改动。

## 核对依据（2026-10-13）

- Codex 全局规则加载：https://learn.chatgpt.com/docs/agent-configuration/agents-md
- Codex MCP 配置：https://learn.chatgpt.com/docs/extend/mcp
- Anthropic Messages：https://platform.claude.com/docs/en/api/messages/create
- Opus 5.5 effort：https://platform.claude.com/docs/en/build-with-claude/effort

本包由 GPT 整理与静态验证；本次未调用 Claude，未使用真实 Key，未在接收方设备安装，未进行真实 API 扣费验证。
