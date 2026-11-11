# Quota login warning

Prints a `Quota status:` block at interactive shell login: quota usage against
soft/hard limits in human-readable units (`quota -s` output), plus a
`*** QUOTA EXCEEDED ***` marker when a limit is being exceeded. Same
functionality as the one running on gpu704 (working copy
`/home/harry/quota-warning.sh` there, installed as
`/etc/profile.d/quota-warning.sh`).

Files:

- `quota-warning.sh` -- the script; sourced from `/etc/profile.d/` at login.
- `apply.sh` -- installs it on the current host (needs root; re-runs are no-ops).

Apply on a host:

    sudo sh apply.sh

Staged for cs at `/home/harry/quota/`:

    ssh cs
    sudo sh /home/harry/quota/apply.sh
    sudo quota -s -g azaaz        # preview a capped group (400G soft / 500G hard)

Behavior notes:

- Only interactive login shells print it; non-interactive shells and ssh
  one-liners stay quiet.
- Sections with no quota records are suppressed, so a user not covered by any
  quota sees nothing.
- On cs the five capped users (azaaz, mrz, zsh, huangwz, ljc) see their personal
  group's line with the 400G/500G limits; other users see nothing.

Uninstall: `sudo rm /etc/profile.d/quota-warning.sh`
