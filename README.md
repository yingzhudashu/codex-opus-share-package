# Codex 全局规则与 Claude Brain 分享包

本仓库用于把一套脱敏后的 Codex 全局工作规则和本地 `claude_brain` MCP 桥接快速安装到 Windows 用户环境，并便于审阅、迁移和维护。

## 先了解当前实现

仓库中有两类 Claude 配置，作用不同，不能混为一谈：

| 项目 | 当前状态 | 身份验证与配置来源 |
| --- | --- | --- |
| `claude_brain` MCP（`plan`、`review`、`diagnose`） | 仓库实际提供并由 `install.ps1` 注册 | 启动本机 Claude Code CLI，复用该 CLI 当前用户的登录态 |
| `opus-config.example.json`、`opus-key.example.env` | 供接收方自行开发或接入 API 工具的模板 | 当前安装器和 `claude_brain_mcp.py` **不会读取**这些模板，也不会发起 Anthropic Messages HTTP 请求 |

因此，安装本包并不等于配置了自定义 API 地址或 API Key。使用内置 MCP 前，需要在本机安装并登录 Claude Code；使用自定义 API 则需要另行实现并注册兼容工具，不能把 API 地址误填成 MCP 服务地址。

## 快速安装

适用范围：Windows、Codex CLI 可从 PowerShell 调用、Python 3 可通过 `python` 或 `py` 命令启动，并且本机已安装 Claude Code。安装器会检查 Python，但 Claude Code 的安装和登录由接收方另行完成。

在解压后的本目录打开 PowerShell：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

安装器会：

1. 使用 `$env:CODEX_HOME`；若未设置，则使用 `$env:USERPROFILE\.codex`。
2. 在 Codex 配置目录创建 `backups\opus-share-时间戳`，仅备份现存的全局 `AGENTS.md`。安装器不会复制或改写 `config.toml`，以免把其中可能存在的认证配置复制到备份目录。
3. 把仓库 `AGENTS.md` 中 `<!-- project-doc -->` 标记之后的规则内容，插入或更新到全局 `AGENTS.md` 的 `# BEGIN/END codex-opus-share-package` 标记块中；标记块外的内容会保留。
4. 将 `claude_brain_mcp.py` 复制到 `<CODEX_HOME>\tools\claude_brain_mcp.py`，执行 Python 编译检查。
5. 移除并重新注册名为 `claude_brain` 的 Codex MCP，启动命令指向所选 Python 和已复制的脚本。

安装器不复制或读取 Key，不配置 Claude Code 登录，不修改 Codex 主模型或推理档位。脚本会提示可在 Codex 中自行设置 `gpt-6.1-sol`；这不是脚本已完成的模型配置。完成后重启 Codex 或新建会话，再检查工具是否加载。

卸载、升级、备份范围和逐项验收见[运维手册](运维手册.md)。

## Claude 工具的项目访问与调用方式

首次调用 `plan`、`review` 或 `diagnose` 时必须传入准确的项目绝对路径 `project_root` 和任务 `task`。桥接会校验该路径是存在的目录，以它作为子进程工作目录，并用 `--add-dir` 授权项目读取。Claude Code 使用 `Read`、`Glob`、`Grep` 只读查看任务相关文件；`context` 只补充任务约束、复现信息或 GPT 已知事实，不要求把整仓库复制进上下文。

命令配置了 `--safe-mode`、只读工具白名单、plan 权限、无权限提示和常见密钥/凭据路径拒绝规则。桥接不提供编辑工具或命令执行工具。Claude Code 自身版本、登录状态和本地安全策略仍会影响实际读取能力；必须依据真实调用结果报告，不得仅凭参数宣称成功。

首次 MCP 调用异步返回 `operation_id` 和 `status=running`。调用方必须使用同名工具和该 ID 轮询，直到 `completed` 或 `failed`；每次最多等待 20 秒，不能重复提交原始任务，也不能把 `running` 当成方案正文。内置桥接单次最多等待 600 秒、整次请求预算为 900 秒。明确返回的暂时性网络/服务端错误最多额外重试一次；桥接本地等待超时不会自动重放，因为上游可能仍在处理原请求。stdio 输入输出显式使用 UTF-8。

档位接受 `medium`、`high`、`max`，并映射到 Claude Code CLI 的 `--effort` 参数。最终返回包含实际正文和非敏感元数据。返回的模型名只是 CLI/网关提供的声明，并非独立的上游身份认证。

## 仓库内容

| 文件 | 用途 |
| --- | --- |
| `AGENTS.md` | 脱敏后的全局规则；`<!-- project-doc -->` 之后是本仓库文档，不会被安装器复制进全局规则 |
| `新客户端接入提示词.md` | 在其他客户端接入规则、工具和自备 API 时使用的任务提示词 |
| `本地配置.example.md` | 接收方私有配置和凭据的填写说明 |
| `opus-config.example.json` | API 接入实现可参考的非敏感配置模板；不由内置桥接读取 |
| `opus-key.example.env` | 空白 Key 模板；不得直接填写真实凭据或作为自动加载机制 |
| `claude_brain_mcp.py` | 本地 Claude Code CLI MCP 桥接 |
| `install.ps1` | Windows 全局规则、桥接复制和 MCP 注册脚本 |
| `test_claude_brain_mcp.py` | 离线单元测试，覆盖项目路径、只读命令参数、异步轮询和错误信息 |
| `运维手册.md` | 安装前检查、验收、排错、安全边界、升级和卸载说明 |

## 离线验证

在仓库根目录执行：

```powershell
python -m unittest -v test_claude_brain_mcp.py
python -m py_compile claude_brain_mcp.py
```

若环境中只有 Python Launcher，可将 `python` 换成 `py -3`。这些命令不会联系 Claude 服务，也不验证 Codex 是否已加载 MCP；真实调用和当前会话加载状态必须分别验收。

## 安全与分享

- 不要把 API Key、Token、Cookie、私钥、真实凭据配置、运行日志或客户数据放进仓库、提示词、聊天、命令行或分享压缩包。
- `.example` 文件只保留占位符。仓库 `.gitignore` 忽略 `opus-config.json`、`opus-key.env` 和常见本地配置，但忽略规则不等于凭据安全措施。
- 若另行使用 API 配置，真实凭据应放在分享目录、版本库和同步目录之外的凭据管理器或受限私有文件中；真实联网/计费验证须有针对本次调用的明确授权。
- 本包中的 SSH 说明是接收方配置约定，不附带服务器地址、账号、私钥或连接授权。

## 文档依据与状态

本仓库文档以安装脚本、桥接实现和离线测试为实现依据。Codex 配置说明的官方入口包括 [AGENTS.md 配置](https://learn.chatgpt.com/docs/agent-configuration/agents-md) 与 [MCP 配置](https://learn.chatgpt.com/docs/extend/mcp)；Claude Messages 与 effort 模板的参考入口见[运维手册](运维手册.md)。链接可用性、产品版本行为和供应商能力应在接收方实际环境中重新核实。

最近维护：2026-10-14。此日期说明文档修改时间，不代表所有依赖的最新版本或新设备验收日期。当前桥接预算与重启后真实调用验收记录见[运维手册](运维手册.md)。
