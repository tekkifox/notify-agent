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
