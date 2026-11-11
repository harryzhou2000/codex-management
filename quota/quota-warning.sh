#!/bin/sh
# Working copy: /home/harry/quota-warning.sh
# Installed copy: /etc/profile.d/quota-warning.sh
# Print quota status at every login (user + group quotas).
# Unconditional: prints even when nothing is critical, but sections
# with no quota records (e.g. groups without quotas) are suppressed.
# Output is `quota -s` verbatim, so values are human-readable (K/M/G/T)
# and the columns stay correctly aligned even when a grace field is blank.

# Only show in interactive login shells; skip otherwise (covers Codex exec shells).
case $- in
  *i*) ;;
  *) return 0 ;;
esac

show_quota() {
  quota -s -w "$@" 2>/dev/null | awk '
    $0 ~ /Disk quotas/ { owner = $0; next }
    $1 == "Filesystem" { fs_header = $0; next }
    $1 ~ /^\// {
      if (owner != "") {
        print owner
        owner = ""
      }
      if (fs_header != "") {
        print fs_header
        fs_header = ""
      }
      print
      if ($0 ~ /\*/ || $0 ~ /\+/)
        print "  *** QUOTA EXCEEDED ***"
    }'
}

output="$(show_quota)
$(show_quota -g)"

if [ -n "$output" ]; then
  printf "\nQuota status:\n%s\n" "$output"
fi
