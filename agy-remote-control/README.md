# Antigravity remote-control lifecycle scripts

These scripts manage the current user's `agy remote-control` daemon without deleting
Antigravity sessions, credentials, or settings. They mirror the Codex app-server lifecycle
tools, with one Antigravity-specific step: proxy variables are written into the systemd
user service environment instead of being left only in the invoking shell.

`agy remote-control start` registers `antigravity-cli-daemon.service`, but the service does
not inherit HTTP proxy variables from the shell that ran the command. `start.sh --proxy`
sources `~/.setproxy.sh`, writes the common proxy variables to a mode-0600 environment file,
and installs a systemd drop-in that makes the daemon read that file. It also verifies the
new daemon process's environment against the selected mode.

## Commands

Inspect the unit, instance name, configuration mode, and process proxy-variable names:

```bash
./agy-remote-control/status.sh
```

Start when no daemon is running:

```bash
./agy-remote-control/start.sh --proxy
./agy-remote-control/start.sh --no-proxy
```

Gracefully stop, unregister, and start with a new network environment:

```bash
./agy-remote-control/replace.sh --proxy
./agy-remote-control/replace.sh --no-proxy
```

`replace.sh` preserves the current Remote Control instance name unless `--name` is given:

```bash
./agy-remote-control/replace.sh --proxy --name gpu704
```

Use `--session` when the daemon should be scoped to the current login session instead of
starting at boot:

```bash
./agy-remote-control/replace.sh --proxy --session
```

Preview a start or replacement without changing the service:

```bash
./agy-remote-control/start.sh --proxy --replace --dry-run
```

If graceful replacement hangs, use the explicit fallback:

```bash
./agy-remote-control/replace.sh --proxy --force-replace
```

Stop and unregister the daemon while keeping all Antigravity data:

```bash
./agy-remote-control/stop.sh
./agy-remote-control/stop.sh --dry-run
```

## Proxy handling

The default proxy source is `~/.setproxy.sh`. Override it when needed:

```bash
PROXY_ENV_FILE=/path/to/proxy-env.sh ./agy-remote-control/replace.sh --proxy
```

The script stores these variables for the service:

```text
HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY
http_proxy https_proxy all_proxy no_proxy
```

Generated files:

```text
~/.config/agy-remote-control/network.env                 # mode 0600, contains proxy values
~/.config/systemd/user/antigravity-cli-daemon.service.d/
  10-network.conf                                       # references the environment file
```

The scripts print only variable names, never their values. Re-run `replace.sh --proxy`
after changing the source proxy file; an already-running daemon keeps the previous copy.

When started through the normal systemd path, `--no-proxy` adds an `UnsetEnvironment=`
drop-in so the service does not inherit proxy variables from the user manager. The same
mode is applied to the `agy` process that performs registration, so the login-scoped
background fallback also receives the selected environment.

## Environment overrides

```bash
AGY_BIN=/path/to/agy \
AGY_UNIT=antigravity-cli-daemon.service \
AGY_MGMT_CONFIG_DIR=/path/to/config \
SYSTEMD_USER_DIR=/path/to/systemd/user \
PROXY_ENV_FILE=/path/to/proxy-env.sh \
  ./agy-remote-control/replace.sh --proxy
```

Replacing the daemon disconnects active Remote Control connections; clients can reconnect
after `agy remote-control status` reports the new daemon active. Do not run `stop.sh` or
`replace.sh` from inside the remote-control daemon itself; the scripts refuse that ancestry.
