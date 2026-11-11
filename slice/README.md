# slice -- per-user CPU limits via systemd user slices

`set-user-slice.sh` sets a CPU limit (systemd `CPUQuota=`) for a list of
users by updating their user slices: `/etc/systemd/system/user-<uid>.slice`

    [Slice]
    CPUQuota=<cores * 100>%

100% is one CPU core, so 32 -> 3200% and 128 -> 12800%. After writing the
files it runs `systemctl daemon-reload`, which applies the limit to running
sessions as well (and every new login).

This is separate from the disk-quota tooling (`../quota/`, the login warning):
nothing here touches /home usage limits.

Default users: `azaaz zsh huangwz mrz ljc` (the cs accounts; uid 1004/1005/1006/1010/1007).
On other machines pass the users explicitly, e.g. `... 32 harry zsh`.

## Usage

    sudo ./set-user-slice.sh 32              # 32 cores -> CPUQuota=3200%
    sudo ./set-user-slice.sh 128             # 128 cores -> CPUQuota=12800%
    sudo ./set-user-slice.sh 32 azaaz mrz    # explicit user list
    sudo ./set-user-slice.sh                 # prompts for the core count
    sudo ./set-user-slice.sh --clear         # remove quota files (unlimited)

    ./set-user-slice.sh --status             # current values (no root needed)
    ./set-user-slice.sh --dry-run 32         # preview (no root needed)

Staged on cs at `/home/harry/slice/`:

    ssh cs
    sudo bash /home/harry/slice/set-user-slice.sh 32

## How to check

    systemctl show user-1010.slice -p CPUQuotaPerSecUSec    # "32s" == 3200%
    cat /sys/fs/cgroup/user.slice/user-1010.slice/cpu.max   # "3200000 100000"

## Notes

- The per-user files only add `CPUQuota`; systemd's shipped defaults for user
  slices (`/usr/lib/systemd/system/user-.slice.d/10-defaults.conf`:
  Description, `StopWhenUnneeded=yes`, `TasksMax=33%`) still apply. Other
  settings already in the file (e.g. `MemoryMax=`) are preserved when the
  quota changes.
- `--clear` removes the files, returning the users to unlimited (any other
  settings in those files go away too).
- This is the scripted version of `sudo systemctl edit --force --full
  user-<uid>.slice` with `[Slice]` + `CPUQuota=...%`.
