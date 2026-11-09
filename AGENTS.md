# AGENTS.md

## Public repository: no real hosts or secrets in documentation

This repository is public. Documentation — `README.md`, any other `*.md`, and any `*.example` env
file — must never contain a real hostname, public IP address, channel/user/tenant/client id, API
key, token or password from the operator's deployment.

- Hosts: use `example.com` subdomains (`host1.example.com`, `archview.example.com`).
- Public IPs: use a TEST-NET address (`203.0.113.10`).
- Ids and secrets: use obviously fake values (`000000000000000000`, `ca_EXAMPLECONNECTION`).
- Describe the shape ("the hosts in `HOST_REPORT_HOSTS`") rather than naming the real thing.
- When a code default is only a convenience and the deployment sets the variable explicitly, the
  default is documentation too: give it a placeholder.

## One host list

Prompts never enumerate SSH hosts: `ssh_probe` defaults to the target's `host_scope`, so the CMS is the only list.

## `/bin/sh` is dash, and dash exits on some redirection errors

`/bin/sh` is dash in the container image and on the CI runner, and it is not bash. A failed
redirection attached to a POSIX *special* builtin (`:`, `.`, `eval`, `exec`, `export`, `readonly`,
`set`, `unset`, ...) aborts a non-interactive shell. Never write `: > "$path" 2>/dev/null` as
"try to write this file": under dash it kills the script where `touch` or a subshell would only
fail the one command. Use `( : > "$path" ) 2>/dev/null` or a regular command, and test a write
inside `if !` when its failure must not change the exit status. Shell tests that exercise such a
path should run the script through `sh` and `bash` explicitly, not rely on its shebang.
