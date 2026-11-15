# Security Policy

## Scope

This tool drives a live 1C:Enterprise Configurator of the machine it runs on.
It has **no network transport** by default (stdio only) and refuses to write to
databases not listed in `own_bases`. Do not list production databases there.

## Reporting

Private vulnerability reports: open a GitHub security advisory ("Report a
vulnerability" on the Security tab) — do not open a public issue.

## Hardening notes

- The bridge intentionally has **no arbitrary code execution** operation; all
  writes go through whitelisted named operations (`send_cfg_cmd` whitelist).
- `read_only: true` in the server config disables every metadata/DB write
  operation (useful for review-only sessions).
- The configurator machine must be trusted: the bridge executes whatever the
  MCP client asks within those named operations.
