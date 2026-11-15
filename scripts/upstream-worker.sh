#!/usr/bin/env bash
# Run only on a disposable GitHub-hosted Linux worker, before vendor actions.
set -euo pipefail
root="${RUNNER_TEMP:?}/nerdtools-worker"
actor=nerdtools-agent
case "${1:-}" in
  prepare)
    plan="${2:?handoff directory required}"
    if id "$actor" >/dev/null 2>&1; then
      echo 'Refusing to reuse an existing agent account' >&2
      exit 1
    fi
    sudo useradd --system --user-group --create-home --home-dir /home/nerdtools-agent --shell /bin/bash "$actor"
    # Traverse private runner ancestors without exposing their directory listings.
    directory="$(dirname "$root")"
    while [[ "$directory" != / ]]; do
      sudo setfacl -m "u:$actor:--x" "$directory"
      directory="$(dirname "$directory")"
    done
    mkdir -p "$root/control" "$root/output" "$root/collected"
    python3 scripts/upstream-runner.py extract --archive "$plan/workspace.tar" \
      --workspace "$root/work" --manifest "$root/control/manifest.json"
    cp "$plan/prompt.md" "$root/control/prompt.md"
    cp scripts/upstream-report.schema.json "$root/control/report.schema.json"
    mkdir -p "$root/work/.upstream-downloads"
    cp "$plan/context.json" "$root/work/.upstream-downloads/context.json"
    sudo install -d -m 755 /opt/nerdtools-controller
    sudo install -m 555 scripts/upstream-runner.py /opt/nerdtools-controller/runner.py
    sudo chown -R root:root "$root/control"
    sudo chmod -R a-w "$root/control"
    sudo chown -R "$actor:$actor" "$root/work" "$root/output"
    sudo chmod 755 /home/nerdtools-agent
    sudo install -d -o "$actor" -g "$actor" -m 755 /home/nerdtools-agent/.codex
    printf 'approval_policy = "never"\n[sandbox_workspace_write]\nnetwork_access = true\n' | sudo tee /home/nerdtools-agent/.codex/config.toml >/dev/null
    # The action's runner-owned proxy writes this before the agent starts.
    sudo install -o "$(id -un)" -g "$(id -gn)" -m 644 /dev/null "/home/nerdtools-agent/.codex/${GITHUB_RUN_ID:?}.json"
    ;;
  claude-proxy)
    # The model UID cannot read the real key from the proxy's config or memory.
    sudo apt-get update -qq
    sudo apt-get install -y --no-install-recommends nginx-light
    sudo "$(command -v npm)" install --prefix /opt/nerdtools-claude --ignore-scripts --no-audit --no-fund @anthropic-ai/claude-code-linux-x64@2.1.325
    sudo --preserve-env=ANTHROPIC_API_KEY python3 - <<'PY'
import os, re
from pathlib import Path
key = os.environ['ANTHROPIC_API_KEY']
if not re.fullmatch(r'[A-Za-z0-9_-]+', key):
    raise SystemExit('Invalid Anthropic API key format')
root = Path('/root/nerdtools-anthropic-proxy')
root.mkdir(mode=0o700)
location = '''
    location = PATH {
      limit_except POST { deny all; }
      proxy_pass https://api.anthropic.com;
      proxy_http_version 1.1;
      proxy_set_header Connection "";
      proxy_set_header Host api.anthropic.com;
      proxy_set_header Authorization "";
      proxy_set_header x-api-key "KEY";
      proxy_ssl_server_name on;
      proxy_ssl_verify on;
      proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
      proxy_intercept_errors on;
      error_page 401 403 = @provider_auth_error;
      proxy_buffering off;
      proxy_read_timeout 300s;
    }
'''
config = '''user www-data;
pid /root/nerdtools-anthropic-proxy/nginx.pid;
error_log /root/nerdtools-anthropic-proxy/error.log warn;
events { worker_connections 64; }
http {
  access_log off;
  client_max_body_size 16m;
  server {
    listen 127.0.0.1:4318;
    location / { return 403; }
    location @provider_auth_error {
      default_type application/json;
      return 401 '{"type":"error","error":{"type":"authentication_error","message":"Provider authentication failed"}}';
    }
'''
for path in ('/v1/messages', '/v1/messages/count_tokens'):
    config += location.replace('PATH', path).replace('KEY', key)
config += '  }\n}\n'
file = root / 'nginx.conf'
file.write_text(config)
file.chmod(0o600)
PY
    sudo nginx -t -c /root/nerdtools-anthropic-proxy/nginx.conf
    sudo nginx -c /root/nerdtools-anthropic-proxy/nginx.conf
    node_dir="$(dirname "$(command -v node)")"
    sudo python3 - "$node_dir" <<'PY'
import shlex, sys
from pathlib import Path
path = Path('/opt/nerdtools-controller/claude')
command = ['/usr/bin/sudo', '-n', '-u', 'nerdtools-agent', '--', '/usr/bin/env', '-i',
           'HOME=/home/nerdtools-agent', 'USER=nerdtools-agent', 'LOGNAME=nerdtools-agent',
           'PATH=' + sys.argv[1] + ':/usr/local/bin:/usr/bin:/bin',
           'ANTHROPIC_API_KEY=local-proxy-placeholder', 'ANTHROPIC_BASE_URL=http://127.0.0.1:4318',
           'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1',
           '/opt/nerdtools-claude/node_modules/@anthropic-ai/claude-code-linux-x64/claude']
path.write_text('#!/bin/sh\nexec ' + shlex.join(command) + ' "$@"\n')
path.chmod(0o555)
PY
    ;;
  stop)
    if id "$actor" >/dev/null 2>&1; then
      test -x /usr/bin/pkill
      sudo /usr/bin/pkill -KILL -u "$(id -u "$actor")" || test "$?" = 1
    fi
    if sudo test -f /root/nerdtools-anthropic-proxy/nginx.pid; then
      sudo nginx -s quit -c /root/nerdtools-anthropic-proxy/nginx.conf
    fi
    ;;
  *) echo 'Expected prepare, claude-proxy, or stop' >&2; exit 2 ;;
esac
