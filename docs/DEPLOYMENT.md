# DEPLOYMENT.md — Deployment Notes

## Trusted Docker / Kubernetes Non-Local Binds

By default, saving `OUROBOROS_SERVER_HOST=0.0.0.0` through the Settings UI
requires `OUROBOROS_NETWORK_PASSWORD` in the same save. This keeps desktop and
local-network launches from accidentally exposing the full Ouroboros HTTP and
WebSocket surface without the built-in password gate.

Trusted container deployments may opt out with:

```bash
OUROBOROS_TRUST_NONLOCAL_BIND_WITHOUT_PASSWORD=1
```

Use this flag only when access is already restricted by external
infrastructure, for example:

- ingress authentication
- VPN-only routing
- private Kubernetes service/network policy
- an authenticated reverse proxy

With the flag enabled, Ouroboros still warns when saving a non-localhost bind
without `OUROBOROS_NETWORK_PASSWORD`, but the Settings UI no longer blocks
ordinary settings saves such as API-key updates. Do not use this flag on an
open LAN or public port.

## Headless Linux server + desktop Remote connection

Run Ouroboros headless on your own Linux server and connect the desktop app to
it over an SSH tunnel. The remote server is a full, independent Ouroboros — its
own identity, memory, budget, and provider keys; the desktop is a thin client.

### Trust posture (read first)

- The server binds **loopback only** (`127.0.0.1`). The SSH tunnel is the sole
  entry point, and SSH is the authentication layer — so no
  `OUROBOROS_NETWORK_PASSWORD` is involved on this path.
- **Single-tenant assumption**: the loopback auth gate trusts every loopback
  client, so any local process on the server (and any local process on your
  desktop while a tunnel is up) can reach the full API. Use a server account
  and a desktop you trust; do not run this on a shared multi-user host.
- **Do not co-host on one machine+port**: the headless server is meant to run on
  a DIFFERENT machine from the desktop app. Do not run both on the same host
  bound to the same port (8765) — the desktop launcher clears its own runtime
  port at startup and would kill a headless server squatting it. One machine =
  one Ouroboros server per port.
  "Any local process" explicitly includes the desktop's own resident Ouroboros
  agent: while a tunnel is up, the local agent — and, like for the LOCAL
  control-plane ports (8765/8766/8767), any shell-capable child it spawns
  (`run_command`) — can reach the remote being's API through the forwarded
  loopback port. That is the same single-tenant trust posture as the rest of
  this section (the workspace is not a network sandbox). The ONE deterministic
  boundary that IS extended to the tunnel: subagent BROWSER navigation. The
  active tunnel port is published to `state/active_tunnel_port` on connect and
  joins `browser._control_plane_loopback_ports` (removed on disconnect), so a
  subagent's browser tool is refused the forward exactly as it is refused the
  local control-plane ports — no more, no less. Shell isolation for children is
  out of scope (and would need OS-level sandboxing, not a command-text
  heuristic).

### Install from source (systemd user unit)

```bash
# on the server, as an ordinary (non-root) user:
git clone https://github.com/razzant/ouroboros.git ~/ouroboros-server/repo
python3 -m venv ~/ouroboros-server/.venv
~/ouroboros-server/.venv/bin/pip install -r ~/ouroboros-server/repo/requirements.txt
# browser tools (optional): ~/ouroboros-server/.venv/bin/python -m playwright install-deps chromium

mkdir -p ~/.config/systemd/user
cp ~/ouroboros-server/repo/packaging/systemd/ouroboros.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ouroboros
loginctl enable-linger "$USER"     # keep it running across logout / before login
```

The unit runs `python -m ouroboros.cli server --host 127.0.0.1 --port 8765`
with `Environment=OUROBOROS_APP_ROOT=%h/ouroboros-server`, so the server's
repo AND data live together under `~/ouroboros-server/` (`repo/`, `data/`) —
without that line the code would run from `~/ouroboros-server/repo` while
data/self-repo paths defaulted to `~/Ouroboros/*` (the desktop location).
Adjust `ExecStart`/`WorkingDirectory`/`Environment` together if you installed
elsewhere (and set the profile's *Remote data dir* on the desktop to match) —
a systemd user manager has no shell `PATH` or activated venv, so both must be
absolute. `Restart=on-failure` covers crashes and the self-restart exit code
42; `RestartPreventExitStatus=99 43` guarantees a panic stop stays stopped
(BIBLE Emergency Stop Invariant) and a lock conflict (exit 43, "another server
already owns this data dir") never becomes a restart loop.

### First-run setup through the tunnel

The setup wizard is served by the gateway, so complete it in a browser over a
temporary tunnel:

```bash
# on your desktop:
ssh -L 8765:127.0.0.1:8765 user@your-server
# then open http://127.0.0.1:8765 in a browser and finish the wizard
```

### Connect the desktop app

1. Set up key/agent SSH auth to the server and verify it once interactively —
   `ssh user@your-server true` — so host-key acceptance and key unlock are done
   (the app uses `BatchMode` and never prompts for passwords or host keys).
2. In the desktop app: **Settings → Advanced → Remote connection → Save
   connection** (name + SSH target; the target may be a `user@host` or an
   `~/.ssh/config` alias, which is also where a non-default port, identity file,
   or `ProxyJump` belong).
3. Click **Connect**. The app opens the tunnel, verifies the server is
   reachable and recent enough, and switches the window to it. The header shows
   a **Remote: …** pill with **Back to local**. If the tunnel drops it
   reconnects automatically for ~2 minutes before returning you to local.

If the server is not running, Connect fails with a clear message; start it with
`systemctl --user start ouroboros`.

### Terminal users (CLI over a manual tunnel)

There is no built-in `ouroboros remote` command in v1. Open the tunnel yourself
and point the CLI at it:

```bash
ssh -fN -L 8765:127.0.0.1:8765 user@your-server
OUROBOROS_URL=http://127.0.0.1:8765 ouroboros run "your task"
```

`--attach` uploads file content to the server, so attachments work across the
tunnel like any local run.
