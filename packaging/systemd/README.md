# Running Ouroboros under systemd

Without a unit file, Ouroboros started from a desktop launcher lands in a
transient `systemd-run` scope with a generated name such as
`run-p497094-i8877010.scope`.  Stopping it then requires discovering that name
first:

```bash
cat /proc/$(pgrep -f '/opt/ouroboros/Ouroboros' | head -1)/cgroup
systemctl --user stop run-p497094-i8877010.scope
```

The name changes on every start, so it cannot be scripted.  `pkill` is not a
substitute: the launcher spawns `server.py` and a pool of workers, and killing
only the parent leaves the workers holding port 8765, so the next start fails
with `address already in use`.

The unit here gives a stable name instead.

## Install

The `.deb` and `.rpm` packages ship this unit at
`/usr/lib/systemd/user/ouroboros.service`, so after a package install it is
already available by name — nothing further is needed:

```bash
systemctl --user start ouroboros
```

### The packages ship the unit but never enable it

This is deliberate. Launching a desktop agent is the user's decision, not the
package manager's: the `.desktop` entry already covers the usual case, and a
package that starts an agent on install would run it for every account on the
machine, including ones that never asked for it.

So installing the package only makes the name available. Two separate actions
follow, and they are not the same:

```bash
systemctl --user start ouroboros          # run it now, once
systemctl --user enable --now ouroboros   # run it now AND on every login
```

Use `enable` only if the agent should come up with the session. To undo it:

```bash
systemctl --user disable --now ouroboros
```

Add `loginctl enable-linger $USER` on top of `enable` if the agent must
survive logout — otherwise the user session, and the unit with it, ends when the
last session closes.

The packaging tests enforce this: `systemctl enable` and `systemctl start` must
not appear in the build scripts (`tests/test_release_proof.py`).

### Other install methods

For a source checkout, an AppImage, or the tarball, install it by hand:

```bash
mkdir -p ~/.config/systemd/user
install -m 644 packaging/systemd/ouroboros.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ouroboros
```

A unit in `~/.config/systemd/user` takes precedence over the packaged one, which
is the supported way to override `ExecStart` for a headless host.

## Use

```bash
systemctl --user start ouroboros
systemctl --user stop ouroboros
systemctl --user restart ouroboros      # picks up local source changes
systemctl --user status ouroboros
journalctl --user -u ouroboros -f
```

## Readiness

`active` in `systemctl` only means the process launched.  Two later milestones
matter, and they are not the same one:

| Signal | Means | Enough to submit a task? |
|---|---|---|
| HTTP answers | web layer up | no |
| `workers=N/N` in `ouroboros status` | worker pool filled | **no** |
| `supervisor_ready: true` in `/api/state` | supervisor up | yes |

Submitting before the supervisor is up is rejected:

```text
error: HTTP 503: supervisor is still starting
```

The authoritative check:

```bash
until curl -sf -m 5 http://127.0.0.1:8765/api/state |
      python3 -c 'import sys,json; sys.exit(0 if json.load(sys.stdin).get("supervisor_ready") else 1)'
do sleep 2; done
```

`/api/state` also carries `supervisor_error`, which explains a supervisor that
never becomes ready.

Scripts that submit work anyway should retry on the 503 rather than poll — the
in-repo benchmark wrapper does exactly that
(`devtools/benchmarks/harness_bench_fast/ouroboros_cli_wrapper.py`): up to 18
retries, ten seconds apart, treating the code as a startup race rather than a
task failure.

`ouroboros status` remains useful for a different question — which revision is
loaded:

```bash
ouroboros status
# Ouroboros 6.96.2 at http://127.0.0.1:8765
# branch=ouroboros sha=160a5c43 workers=10/10
```

The `sha=` field confirms a restart actually picked up edited local sources.

## Why a user unit and not a system one

State lives in `$HOME/Ouroboros` and the desktop build needs the user's
session.  A system unit runs as a different user and would silently use a
different data directory.

Run `loginctl enable-linger $USER` if the agent must keep running after
logout.

## Headless hosts

Replace `ExecStart` with the CLI server:

```ini
ExecStart=/usr/local/bin/ouroboros server
```

Both entry points share the same `$HOME/Ouroboros` state directory.

## Notes on the settings in the unit

`KillMode=control-group` is what makes a plain `stop` sufficient — SIGTERM
reaches the whole process group, workers included.

`TimeoutStopSec=120` leaves room for a tool call in flight; a long prompt on a
modest GPU takes minutes, and a shorter timeout turns a normal stop into a
`SIGKILL`.

`StartLimitBurst=5` / `StartLimitIntervalSec=300` stop a crash loop instead of
letting it run indefinitely.  A port conflict otherwise produces dozens of
restarts and buries the first, real error in the journal.
