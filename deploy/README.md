# Production deployment

The commands below assume an Ubuntu-like host and root access. Review paths
before running them.

## 1. Install code and runtime

Create a dedicated system user and place a reviewed repository checkout at
`/opt/qwen-bridge`. The service user needs read access to the code but must not
own files that deployment automation replaces.

```bash
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin qwen-bridge
sudo install -d -o root -g qwen-bridge -m 0750 /opt/qwen-bridge
sudo install -d -o root -g qwen-bridge -m 0750 /opt/qwen-bridge/secrets
cd /opt/qwen-bridge
python3 -m venv .venv
./.venv/bin/pip install --require-hashes -r requirements.lock.txt
./.venv/bin/pip install --no-deps .
```

## 2. Provision credentials

Generate a bridge key independently from the HA token. Do not paste either into
the unit file, shell history, Git, comments, or logs.

```bash
sudo install -o qwen-bridge -g qwen-bridge -m 0600 /dev/null \
  /opt/qwen-bridge/secrets/ha_token
sudo install -o qwen-bridge -g qwen-bridge -m 0600 /dev/null \
  /opt/qwen-bridge/secrets/bridge_api_key
```

Write the values using a secret manager or an interactive root-only editor.
Copy `.env.example` to `/etc/qwen-ha-bridge.env`, keep file-backed credential
paths, and set `ALLOWED_HOSTS=ha.qwen.xsnight.cn,localhost,127.0.0.1`.

## 3. Start read-only first

Keep `ENABLE_WRITE_TOOLS=false`, install `qwen-bridge.service`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now qwen-bridge
sudo systemctl status qwen-bridge
sudo journalctl -u qwen-bridge -n 100 --no-pager
```

Install/reload the Caddyfile and confirm `https://ha.qwen.xsnight.cn/healthz`.
Configure the Qwen MCP server with an `Authorization: Bearer <bridge key>`
header, refresh tools, and verify `bridge_status`.

## 4. Enable controlled writes

Only after authenticated reads pass, set `ENABLE_WRITE_TOOLS=true` and a small
`ALLOWED_HA_SERVICES` list. Restart, refresh the tool catalog, invoke
`call_ha_service` with `dry_run=true`, then test one reversible device action.

## 5. Network cutover

The Python process must listen on `127.0.0.1:8000`. Remove any firewall rule
that exposes TCP/8000 publicly. Public ingress should be limited to SSH, HTTPS,
and HTTP if Caddy needs HTTP-01 certificate renewal.

## Rollback

Retain the previously deployed release directory or Git commit. Roll back the
code, run `systemctl restart qwen-bridge`, and verify read-only tools before
re-enabling writes. Credential files stay outside Git and do not need to move.
