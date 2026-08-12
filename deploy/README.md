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

Install/reload the Caddyfile, run the HTTP/3 regression guard below, and confirm
`https://ha.qwen.xsnight.cn/healthz`. Configure the Qwen MCP server with an
`Authorization: Bearer <bridge key>` header, refresh tools, and verify
`bridge_status`.

## 4. Enable controlled writes

Only after authenticated reads pass, configure both a small service allowlist
and an exact entity allowlist. Aliases are optional, but let the existing MCP
tool catalog carry common spoken names without adding another tool:

```dotenv
ENABLE_WRITE_TOOLS=true
ALLOWED_HA_SERVICES=climate.turn_on,climate.turn_off,climate.set_temperature,light.turn_on,light.turn_off
ALLOWED_HA_ENTITIES=climate.living_room,light.desk
HA_DEVICE_ALIASES_JSON='{"空调":"climate.living_room","客厅空调":"climate.living_room","挂灯":"light.desk"}'
CLIMATE_TEMPERATURE_MIN=16
CLIMATE_TEMPERATURE_MAX=30
```

Use deployment-specific entity IDs in `/etc/qwen-ha-bridge.env`; do not commit
them or credentials to the repository. Restart the bridge, refresh the Qwen
tool catalog, and confirm `call_ha_service` exposes the intended services,
entities, and aliases. Run `dry_run=true` for each permitted action class.
Leave real device actuation to the owner during the physical-device test, and
keep the MCP/Skill drafts unpublished until that test passes.

## 5. Network cutover

The Python process must listen on `127.0.0.1:8000`. Remove any firewall rule
that exposes TCP/8000 publicly. Public ingress should be limited to SSH, HTTPS,
and HTTP if Caddy needs HTTP-01 certificate renewal.

## HTTP/3 regression guard

The Qwen execution client (okhttp/4.12.0) honors the `Alt-Svc: h3=":443"`
advertisement and upgrades subsequent MCP requests to HTTP/3 (QUIC) on UDP/443.
From Qwen's network that UDP path is unreachable, so the QUIC handshake hangs
until the client timeout and the request never reaches this server — surfacing
as "MCP 工具执行超时" with no Caddy access-log entry. The Caddyfile therefore
disables HTTP/3 (`servers { protocols h1 h2 }`) and strips `Alt-Svc`.

Run the guard after every Caddy change or redeploy to catch a regression:

```bash
# Static check of the file in the repo (no network needed):
bash scripts/verify_caddy.sh deploy/Caddyfile

# Live check against the public endpoint + local UDP/443 listener
# (run this ON the deployment host so the ss check sees the real listener):
bash scripts/verify_caddy.sh deploy/Caddyfile https://ha.qwen.xsnight.cn/mcp
```

The guard exits non-zero if HTTP/3 could be re-enabled (missing `protocols h1
h2`, missing `-Alt-Svc`, a live `Alt-Svc` header, or a UDP/443 listener). Wire it
into any deploy automation so a future `caddy adapt`/reload cannot silently turn
QUIC back on. `ENABLE_WRITE_TOOLS` and all credentials are untouched by this step.

## Rollback

Retain the previously deployed release directory or Git commit. Roll back the
code, run `systemctl restart qwen-bridge`, and verify read-only tools before
re-enabling writes. Credential files stay outside Git and do not need to move.
