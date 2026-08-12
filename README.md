# QwenGlass-HA

一个用于连接千问 / 夸克眼镜与 Home Assistant 的防御性 MCP 网桥。它把 HA
实体查询和经过显式授权的服务调用暴露为 Streamable HTTP MCP 工具，同时针对千问
执行客户端的实际行为保留兼容模式。

## 安全基线

- Python 服务默认只监听 `127.0.0.1:8000`，公网仅通过 Caddy HTTPS 进入；部署后应
  关闭防火墙上的公网 `tcp/8000`。
- MCP 默认要求独立的桥接 Bearer Token。它和 HA 长期令牌分开保存，并且都只能从
  权限为 `0600` 的文件读取，永远不写入 Git、参数或日志。
- 写工具默认关闭。开启时必须同时启用认证，并配置精确的 HA 服务 allowlist。
- HA `GET` 在传输错误时最多安全重试一次；可能操作设备的 `POST` 永不自动重试，
  避免一次指令执行两遍。
- 请求体、HA 响应、服务参数、并发量和旧协议 session 都有上限。
- 不记录请求头、请求体、参数值或令牌；日志仅包含请求 ID、路径、状态、耗时和工具名。

## MCP 工具

默认只暴露三个只读工具：

- `bridge_status`：检查 HA 可达性和令牌状态；
- `list_ha_states`：分页列出实体，可按 domain 过滤；
- `get_ha_state`：读取一个合法 `entity_id` 的完整状态。

只有 `ENABLE_WRITE_TOOLS=true` 且服务位于 `ALLOWED_HA_SERVICES` 时，才会额外暴露
`call_ha_service`。该工具支持 `dry_run=true`，实际调用不会自动重试。

## 千问兼容性

MCP 的 POST 响应使用有 `Content-Length` 的关闭式 `application/json`。旧客户端可走
`initialize → notifications/initialized → tools/*`；在 `MCP_RELAXED_SESSIONS=true`
时，也接受千问执行端偶发省略 initialized 通知或 session header 的调用。认证不存放在
session 中，因此兼容模式不会绕过 Bearer Token。

服务同时验证客户端传入的 `Mcp-Method` / `Mcp-Name`（若存在）是否和 JSON-RPC
正文一致，并支持 `DELETE /mcp` 清理旧 session。由于本服务不发送主动通知，
`GET /mcp` 明确返回 405，移除了此前未被千问使用的双通道 SSE 补丁。

## 本地开发

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync
uv run ruff check .
uv run pytest
```

运行一个本地实例：

```bash
cp .env.example .env
mkdir -p secrets
openssl rand -base64 48 > secrets/bridge_api_key
printf '%s' "$HA_LONG_LIVED_TOKEN" > secrets/ha_token
chmod 600 secrets/bridge_api_key secrets/ha_token
set -a; source .env; set +a
HA_TOKEN_FILE="$PWD/secrets/ha_token" \
BRIDGE_API_KEY_FILE="$PWD/secrets/bridge_api_key" \
ALLOWED_HOSTS="localhost,127.0.0.1" \
uv run qwen-ha-bridge
```

在另一个终端验证：

```bash
BRIDGE_API_KEY_FILE="$PWD/secrets/bridge_api_key" \
uv run python scripts/verify_mcp.py http://127.0.0.1:8000/mcp
```

## 服务器部署

详细步骤见 `deploy/README.md`。生产切换的关键顺序是：

1. 生成独立 `bridge_api_key`，不要复用 HA token；
2. 在千问自定义 MCP 配置中加入 `Authorization: Bearer ...`；
3. 先保持写工具关闭，完成 `initialize / tools/list / bridge_status` 验收；
4. 再按需开启少量服务 allowlist，先用 `dry_run=true` 验证；
5. 关闭公网 `8000`，只保留 Caddy 的 443（以及证书续期所需的 80）。

阿里云官方文档确认远程 MCP 可配置 `Authorization` header，且 HTTP 401/403 是受支持
的鉴权结果：

- <https://help.aliyun.com/zh/model-studio/custom-mcp>
- <https://help.aliyun.com/zh/model-studio/mcp-faq>

## 配置

完整模板见 `.env.example`。生产环境不要把真实密钥直接放进 `.env`；这里只配置密钥
文件路径。应用会在每次请求时读取文件，因此轮换密钥无需重启。

## 健康检查

`GET /healthz` 仅代表进程存活，不返回 HA 地址、实体或认证状态。真实 HA 状态应通过
已认证的 `bridge_status` 工具检查。
