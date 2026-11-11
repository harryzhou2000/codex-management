# Codex app-server lifecycle scripts

These scripts manage the current user's Codex app-server processes without deleting session,
configuration, authentication, or rollout files.

Run lifecycle scripts from an independent terminal or SSH shell. Do not invoke `purge.sh` or
`replace.sh` from a Codex conversation served by the daemon being replaced: terminating the daemon
would terminate the command before it can start the replacement. The scripts detect this ancestry
and refuse when possible.

## Commands

Inspect the managed daemon, configuration timestamp, app-server processes, roles, and inherited
proxy-variable names:

```bash
./codex-app-server/status.sh
```

Preview or stop every verified Codex app-server process owned by the current user:

```bash
./codex-app-server/purge.sh --dry-run
./codex-app-server/purge.sh
```

The default purge is graceful-first: it requests a managed-daemon shutdown, sends `TERM` to any
verified leftovers, waits up to eight seconds, and uses `KILL` only for processes that remain.
Immediate forceful cleanup is explicit:

```bash
./codex-app-server/purge.sh --force --dry-run
./codex-app-server/purge.sh --force
```

Start only when no app-server process exists:

```bash
./codex-app-server/start.sh --proxy
./codex-app-server/start.sh --no-proxy
```

Replace all current instances and load the current `~/.codex/config.toml`:

```bash
./codex-app-server/replace.sh --proxy
./codex-app-server/replace.sh --no-proxy
```

Use `--force-purge` only when graceful replacement is stuck:

```bash
./codex-app-server/replace.sh --proxy --force-purge
```

Proxy mode sources `PROXY_ENV_FILE`, defaulting to `~/.setproxy.sh`. Override paths or the Codex
binary through environment variables:

```bash
PROXY_ENV_FILE=/path/to/proxy-env.sh CODEX_BIN=/path/to/codex \
  ./codex-app-server/replace.sh --proxy
```

Remote Control is separate from the SSH `codex app-server proxy` bridge. The latter forwards a
remote desktop App connection over SSH to the local daemon's Unix socket. Remote Control creates
an additional outbound ChatGPT Remote connection for paired devices. Lifecycle scripts preserve
the existing Remote Control setting unless passed one of:

```bash
./codex-app-server/replace.sh --proxy --enable-remote-control
./codex-app-server/replace.sh --proxy --disable-remote-control
```

## Shared feature settings

The managed daemon is shared by every attaching client (the new Codex TUI, Codex Desktop, SSH
proxy bridges). The TUI checks four shared server features against the daemon on attach and offers
a restart when they differ:

- `api_key_model_discovery`
- `auth_elicitation`
- `code_mode_host`
- `mcp_oauth_refresh_coordination`

`start.sh`/`replace.sh` launch the daemon with the current `~/.codex/config.toml`, so the durable
place to pin these is the `[features]` table in that file:

```toml
[features]
api_key_model_discovery = true
auth_elicitation = true
code_mode_host = true
mcp_oauth_refresh_coordination = false
```

Feature keys present in `config.toml` are protected: runtime
`experimentalFeature/enablement/set` calls from other clients (for example Codex Desktop) cannot
override them. In Codex 0.162.0, a fresh `codex app-server daemon start` replaces the
`featureOverrides` map saved in `~/.codex/app-server-daemon/settings.json` (which backs the TUI's
"Restart with these settings" button), so keep the authoritative values in `config.toml` rather
than relying on that button.

Replacing the daemon disconnects existing TUIs and SSH proxy bridges. Clients must reconnect after
the new daemon is ready.
