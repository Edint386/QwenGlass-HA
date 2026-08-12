#!/usr/bin/env bash
#
# verify_caddy.sh — regression guard for the Caddy deployment.
#
# Purpose: ensure the deployed Caddyfile does NOT advertise HTTP/3 (QUIC), which
# previously caused the Qwen MCP client (okhttp/4.12.0) to hang on an unreachable
# UDP/443 path and report "MCP 工具执行超时" with no server-side access log.
#
# Checks performed:
#   1. Static: the Caddyfile enables `protocols h1 h2` (HTTP/3 disabled).
#   2. Static: the Caddyfile strips the `Alt-Svc` response header.
#   3. Optional, live (only if a URL is passed): the public endpoint does not
#      return an `Alt-Svc` header and (if `ss` is available) no UDP/443 listener
#      is open on the host.
#
# Exit status: 0 = OK, 1 = regression detected, 2 = usage/tooling error.
#
# Usage:
#   scripts/verify_caddy.sh [PATH_TO_CADDYFILE] [LIVE_URL]
#   scripts/verify_caddy.sh deploy/Caddyfile https://ha.qwen.xsnight.cn/mcp

set -euo pipefail

CADDYFILE="${1:-deploy/Caddyfile}"
LIVE_URL="${2:-}"

err() { printf '%s\n' "FAIL: $*" >&2; }
ok()  { printf '%s\n' "OK:   $*"; }

if [[ ! -f "$CADDYFILE" ]]; then
    err "Caddyfile not found: $CADDYFILE"
    exit 2
fi

rc=0

# 1) HTTP/3 disabled via global server protocols.
if grep -Eq '^[[:space:]]*protocols[[:space:]]+h1[[:space:]]+h2[[:space:]]*$' "$CADDYFILE"; then
    ok "Caddyfile disables HTTP/3 (found 'protocols h1 h2')"
else
    err "Caddyfile is missing 'protocols h1 h2' in a global 'servers' block — HTTP/3 may be re-enabled"
    rc=1
fi

# 2) Alt-Svc header stripped.
if grep -Eq '^[[:space:]]*-Alt-Svc[[:space:]]*$' "$CADDYFILE"; then
    ok "Caddyfile strips the Alt-Svc response header"
else
    err "Caddyfile does not strip 'Alt-Svc' — clients may be steered onto QUIC"
    rc=1
fi

# 3) Optional live checks.
if [[ -n "$LIVE_URL" ]]; then
    if command -v curl >/dev/null 2>&1; then
        hdr="$(curl -sS -I --max-time 10 "$LIVE_URL" 2>/dev/null || true)"
        if printf '%s\n' "$hdr" | grep -Eqi '^[[:space:]]*alt-svc:'; then
            err "Live endpoint still advertises Alt-Svc: $LIVE_URL"
            rc=1
        else
            ok "Live endpoint does not advertise Alt-Svc: $LIVE_URL"
        fi
    else
        err "curl not available — skipping live Alt-Svc check"
        rc=2
    fi

    if command -v ss >/dev/null 2>&1; then
        if ss -ulnp 2>/dev/null | grep -Eq '[:]443[[:space:]]'; then
            err "A UDP/443 (QUIC) listener is still open on this host"
            rc=1
        else
            ok "No UDP/443 (QUIC) listener open on this host"
        fi
    else
        err "ss not available — skipping UDP/443 listener check"
        rc=2
    fi
fi

if [[ "$rc" -eq 0 ]]; then
    ok "Caddy HTTP/3 regression guard passed."
else
    err "Caddy HTTP/3 regression guard FAILED (rc=$rc)."
fi
exit "$rc"
