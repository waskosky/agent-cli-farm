# Physical disk protection

Disk protection is independent of memory/thermal choices and defaults off.
Enable it explicitly with `codex-storage configure --enable`. Ordinary setup
copies helpers without installing tasks, activating enforcement or pruning files.
Interactive agents always remain available for diagnosis.

## Measurements and policy

`codex-storage status --path /existing/output` reports the Linux filesystem and
physical Windows backing volume. Under WSL it identifies the exact distribution
from WSL_DISTRO_NAME and the registry; it never trusts a virtual 1 TiB free-space
report as physical capacity. Other attached Linux filesystems require a verified
mapping and otherwise fail closed. Native Linux uses its mounted filesystem.

Initial thresholds are 60 GiB warning, 25 GiB admission reserve, 10 GiB emergency,
and 40 GiB sustained recovery for 120 seconds. They are configurable using
`configure --warning-gib ... --reserve-gib ... --emergency-gib ...
--recovery-gib ... --recovery-seconds ...`. At least 10,000 free inodes are
required when the filesystem exposes inode capacity.

New batch jobs reserve their **peak additional** bytes, including raw stages,
temporary packaging, rollback copies and final output:

```sh
codex-job run --role batch --scope required --storage-path /existing/scratch \
  --reserve-disk-gib 16 --stop-on-disk-pressure -- python3 render.py
```

Reservations are committed under the existing private job lock and grouped by
physical volume. Active and unreadable owners retain their claims. The entire
peak remains reserved until completion, so accounting is intentionally
conservative as files grow. Memory bypass flags do not bypass disk protection.
These are admission promises, not allocated blocks or filesystem quotas.
Other users, distros and unmanaged tools are not part of the reservation ledger.

The optional stop flag separately authorizes identity-verified termination of
that dedicated batch group at emergency pressure or unknown measurements.
It requires a scope and is never accepted for agents. The supervisor checks
every five seconds. Existing TERM/grace/KILL handling preserves process identity
checks and never selects processes by name. Arbitrary programs may require
recovery after termination; use cooperative checks for resumable writers.

A cooperating writer can use `codex-storage lease --owner-pid PID --path DIR
--additional-bytes N`, retain the returned job_id, run
`codex-storage check --running --path DIR` at bounded checkpoints, then call
`release --job-id ID`. Only the direct caller can acquire/release its lease.
Exit 75 holds the writer; exit 2 means invalid settings/helper failure. Preserve
accepted outputs/checkpoints and surface the failure. Do not silently disable
protection or start a replacement rendition. Leases do not grant CPU remedies.

## Windows monitor independent of WSL

From PowerShell, preview then install the unprivileged current-user task:

```powershell
$source = '\\wsl.localhost\Ubuntu\home\USER\bin\codexfarm-storage-windows'
& "$source\install.ps1"
& "$source\install.ps1" -Apply
```

The actual Linux-installed copy is under `~/bin/codexfarm-storage-windows`;
from Windows use its `\\wsl.localhost\DISTRO\home\USER\bin\...` path, or
copy these two source scripts to Windows before installation. Installation puts
the running monitor under Windows LOCALAPPDATA\AgentCliFarm\storage, independent
of Ubuntu. No hardware drivers or administrator privileges are required.

Configure its snapshot's corresponding Linux path explicitly:

```sh
codex-storage configure --enable \
  --windows-snapshot /mnt/c/Users/USER/AppData/Local/AgentCliFarm/storage/snapshot.json
```

The monitor measures fixed Windows drives every ten seconds, maps distributions
to VHD backing drives, records VHD file length and recent drive growth, and
atomically publishes a bounded snapshot. History has two approximately 1 MiB
segments. A logon trigger plus one-minute retry trigger recovers only this task;
MultipleInstances=IgnoreNew prevents duplicates. Disabled/operator-modified tasks
are preserved. Snapshots older than 30 seconds never admit enrolled work.
Without an enrolled snapshot, the CLI uses a bounded direct Windows probe.

`codex-health` reports enabled disk protection and low physical headroom.
The snapshot also guards the Windows system volume and configured WSL swap drive.
Use its drive counters for other Windows consumers too.
A monitor can alert and gate participating writers; it cannot guarantee free
space against arbitrary Windows programs or unmanaged writes.

## Diagnostic logs and maintenance

Legacy tmux terminal mirrors now use `codex-pane-log`: each remains bounded to
16 MiB, retaining the recent 4 MiB on rollover. Provider conversation databases
and deep-history records are separate. The optional deep-history backend's
active mirror retains its upstream behavior; inactive terminal mirrors use the
same conservative expiration command.

`codex-storage prune-logs` previews 30-day expiration of recognized inactive
terminal mirrors; `--apply` deletes eligible files. Active/unreadable writers,
links and unknown filenames survive. The 1 GiB aggregate target reports excess
retention when protected/recent logs prevent cleanup. It is not a hard quota.
No log expiry is scheduled by ordinary setup.

The machine operations repository owns the separately installed daily timer for
completed worktrees, pip/npm download-cache budgets and inactive pane logs.
Repositories own scratch retirement: manifests, live locks and explicit pins
must distinguish disposable output from source, accepted evidence and databases.
Never recursively purge a directory merely because it is named temp.

Linux deletion and Windows reclamation are separate measurements. Inspect both
Linux usage and host free space/VHD length. Trim may make guest free blocks
reclaimable; offline VHD compaction requires an announced maintenance window,
clean WSL shutdown and validation afterward. Do not automatically stop WSL,
shrink live images, or enable sparse VHD changes as emergency cleanup.
