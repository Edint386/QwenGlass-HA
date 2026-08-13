# Security policy

## Secrets

Never commit a Home Assistant token, bridge API key, private key, `.env`, log,
or exported runtime directory. Runtime credentials must be separate regular
files with mode `0600`. The application intentionally rejects group/world
readable credential files.

If a secret enters Git history, deleting the visible file is insufficient:
revoke and rotate it first, then rewrite history if appropriate.

## Public exposure

Only Caddy should be reachable from the internet. The Python service binds to
loopback by default. Do not expose port 8000 through the host firewall.

Write tools are disabled by default. Enabling them requires bridge
authentication and a non-empty, explicit Home Assistant service allowlist.
Prefer individual services such as `light.turn_on`; use `domain.*` only after a
deliberate review.

Control diagnostics may log an allowlisted entity ID, requested service name,
state labels, HTTP status, timing, and a random request ID. They must never log
service payload values, request bodies, authorization headers, or credentials.

## Reporting

Do not open a public issue containing credentials, entity inventories, access
logs, IP addresses, or reproductions that operate a real device. Report those
privately to the repository owner and rotate any potentially exposed secret.
