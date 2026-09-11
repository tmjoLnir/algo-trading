# Hosting this on your own Mac

> **This is the deployment.**
> [ADR 0033](adr/0033-the-mac-stays-and-the-condition-becomes-a-number.md) keeps
> the paper host here and supersedes
> [ADR 0032](adr/0032-the-paper-host-moves-off-the-mac.md), which had moved it
> to an Oracle Cloud A1 — a cutover that was decided and never performed.
> Nothing was provisioned there.
>
> **§1 is the whole of why this is conditional**, and 0033 turns that condition
> into a number you can read in two seconds rather than a paragraph you can
> agree with. If that number moves during a session,
> [ORACLE_HOSTING.md](ORACLE_HOSTING.md) is the route out and 0032 is the
> reasoning behind it — already written, so nobody has to re-argue it.

[ADR 0021](adr/0021-the-paper-host-is-the-operators-own-mac.md) chose the
operator's own macOS machine as the paper host, and ADR 0033 keeps it there.
[DEPLOYMENT.md](DEPLOYMENT.md) is still **the** procedure and this does not
replace it.

**This document is a delta.** It records only the steps where a Mac under a desk
differs from the rented Linux VM DEPLOYMENT.md was written for, and deliberately
does not restate the parts that are identical — two copies of a deploy procedure
is one procedure that will be wrong. Read DEPLOYMENT.md; consult this where it
tells you to.

Six things differ, and one of them decides whether a paper week is possible at
all.

| # | DEPLOYMENT.md says | Here |
|---|---|---|
| 1 | The host stays up | **The Mac sleeps.** This is the one that matters — §1 |
| 2 | `curl get.docker.com \| sh`, dockerd under systemd | Docker Desktop, which is a VM and does not start itself — §2 |
| 3 | x86-64 | arm64 on Apple Silicon — §3 |
| 4 | `git clone /opt/atp`, run as root | Your own checkout, your own user — §4 |
| 5 | Provider snapshots are the recovery story | There is no provider — §6 |
| 6 | `sops-v3.9.4.linux.amd64` | The darwin build, or Homebrew — §4 |

Everything else — `make deploy`, the overlay, `make check-bindings`,
`ATP_WEB_BIND_ADDR`, the halt-before-deploy sequence, `scripts/status.py` — is
unchanged, and DEPLOYMENT.md is where it lives.

---

## 1. Sleep

**A Mac on defaults cannot hold a paper week.** This is not a warning to file
away; it is the reason ADR 0011 rejected a laptop, and the whole of what
ADR 0021 had to answer before choosing one.

What sleep does to *this* stack, specifically:

- The Alpaca WebSocket drops. On wake the ingestor reconnects and backfills the
  gap over REST, which is the design — but only for a gap it can still fetch.
- `StalenessMonitor` measures silence in **wall-clock seconds** against the
  exchange calendar. A four-hour sleep is four hours of silence that arrives
  all at once.
- The five-minute reconcile and the one-minute snapshot both stop. The
  dashboard's `book_as_of` ages while nothing publishes.
- The clock jumps. CLAUDE.md §5 calls anything reading wall-clock time directly
  the hardest class of bug in this system to notice, and a suspended VM is that
  case delivered on purpose.

**The direction of the failure is correct.** The watchdog halts trading, the
kill switch fails closed, and positions keep their broker-side stops throughout
(`docs/SAFETY.md` layer 5). You will not wake up to a runaway. You will wake up
to a halted platform and a gap in the week — and a paper week with a gap in it
is not the four weeks `docs/SAFETY.md` asks for.

### Preventing it

For a single session, from a terminal you leave open:

```bash
caffeinate -dimsu
```

`-d` display, `-i` idle, `-m` disk, `-s` system, `-u` declares the user active.
Note that `-s` asserts only while on AC power — a MacBook on battery will still
sleep. Ctrl-C ends the assertion, and so does closing the terminal, which is the
point: it is scoped to the session rather than a permanent change to the
machine.

For a machine that is meant to stay up:

```bash
sudo pmset -c sleep 0
pmset -g
pmset -g assertions
```

`sleep 0` means never idle-sleep while on AC. `pmset -g` reads back what is
actually set, and `pmset -g assertions` says what is holding the machine awake
right now, and which process is doing it.

On an Apple Silicon laptop that also needs to survive the lid closing, the
setting to look for is `disablesleep`:

```bash
sudo pmset -c disablesleep 1
```

Confirm it took rather than assuming — `pmset -g` prints the resolved settings,
and the behaviour of that flag has varied across macOS releases.

### Confirming the setting took

Read it back. Do not assume it — the behaviour of `disablesleep` has varied
across macOS releases, and "I ran the command" is the belief that cost day 3.

```bash
pmset -g
```

Two lines decide it:

```
System-wide power settings:
 SleepDisabled		1
Currently in use:
 sleep                0 (sleep prevented by powerd, sharingd, bluetoothd)
```

`SleepDisabled 1` is the one to want: it is **system-wide**, so it holds with
the lid closed, which `pmset -c sleep 0` on its own does not. If `SleepDisabled`
is absent from the output entirely, the flag did not take on this release and
`pmset -g assertions` will say what is and is not holding the machine awake.

Two things the same output is worth reading for. **`sleep 0` reflects the
current power source** — `-c` scopes to the charger, so unplugging can restore a
battery value; keep the host on AC. And **`disksleep`** is usually still
non-zero: harmless for the internal SSD, and not harmless if `ATP_BACKUP_DIR`
points at an external volume that can spin down between jobs.

### Verifying it held

**The check that matters is not "did I set the flag" but "did the machine stay
awake through a session"**, and that is a different reading taken at a different
time. ADR 0021 died in the gap between the two.

`pmset -g log` ends with the number that answers it:

```bash
pmset -g log | grep -i "Total Sleep/Wakes"
```

```
Total Sleep/Wakes since boot at 2026-09-11 06:57:50 +0800 :5
```

**That integer is the invariant** ([ADR 0033](adr/0033-the-mac-stays-and-the-condition-becomes-a-number.md)).
It counts since boot, so it never resets: note it before a session and read it
after, and **it must not have moved.** Five, in the reading above, were five
sleeps in the six and a half hours *before* the setting was applied — which is
what the problem looks like when it is still happening.

Read it with the platform's own view of the same window:

```bash
uv run python scripts/status.py
```

That says whether a gap reached the bars. The integer says whether the host is
why.

And the one that catches the quiet version — the VM's clock disagreeing with the
host's after a suspend:

```bash
date -u
docker compose exec -T api date -u
```

Those two should agree to within a second. If they do not, everything the
platform timestamps is suspect until the daemon is restarted, and a restart is
the fix.

> **Do not paste these with trailing `#` comments.** macOS's default interactive
> shell is zsh, which does not treat `#` as a comment unless
> `setopt interactivecomments` is set — so a commented command line arrives at
> `pmset` and `tail` as arguments and fails in a way that looks like the tool is
> broken. Every block in this document is written without them for that reason.

### The session runs while you are asleep

Worth stating because nothing else in this document assumes it, and it changes
what the sleep setting has to survive.

US regular trading hours are 13:30–20:00 UTC. Convert that to the host's local
time before planning anything around it — from **UTC+8 it is 21:30–04:00**, so
the entire session is overnight, unattended, with the machine likely shut and
nobody near it. Day 3's capture ran 19:08 to 05:41 local on exactly that clock.

Three consequences:

- **`SleepDisabled` is the setting that matters**, not `sleep 0`, because the
  lid will be closed. §1 says this already; this is why.
- **The alert path is the only channel that exists** for those hours. A halt at
  02:00 reaches nobody unless `scripts/check_alerts.py --by "you"` has been run
  from this host *and somebody watched a phone light up*. On a host where the
  operator is awake during the session this is a checklist line; here it is the
  whole of docs/SAFETY.md's "alerts reach a human" layer.
- **The verification is a morning ritual, not a live one.** The integer above and
  `scripts/status.py` are read after the close, over coffee. Nothing will tell
  you during the session, because `host_dark_minutes` does not exist yet —
  docs/paper-week/day-3-review.md asked for it and it is still the most valuable
  unbuilt thing here.

## 2. Docker Desktop

Three settings, all in Docker Desktop's own preferences, none of which have an
equivalent on the Linux host DEPLOYMENT.md assumes.

**Start it at login.** Settings → General → "Start Docker Desktop when you sign
in to your computer". `restart: unless-stopped` acts on containers once the
daemon is running; it cannot start the daemon. Without this, a restart of the
Mac leaves you with a stack that is down and a compose file that promises it
comes back — which is exactly the "came back in pieces and looked alive"
failure ADR 0011 documented, arriving through a different door.

**Give it enough memory.** Settings → Resources. DEPLOYMENT.md's sizing table
describes a *host*; on macOS what the stack actually gets is what Docker Desktop
is permitted, and the gap between the two is invisible until something is
OOM-killed. That table's 10-symbol five-year minute-bar backtest needs 4.2 GB
for the bar objects alone, before pandas or the engine's own state. A Mac with
32 GB and Docker Desktop capped at 4 GB will kill it every time.

Postgres compounds this: `timescaledb-tune` sizes `shared_buffers` from the RAM
it can see, which is the VM's, not the Mac's.

**Watch the disk.** The VM's disk image grows and does not shrink on its own:

```bash
docker system df
docker system prune
docker builder prune
```

`df` says what is actually using the space; `system prune` clears dangling
images and stopped containers; `builder prune` clears the build cache, which is
usually the big one.

`make deploy` passes `--build`, so every deploy adds layers. DEPLOYMENT.md
budgets 15–20 GB for Docker before a single bar is stored, and that number is
the same here.

## 3. Architecture

```bash
uname -m
```

`arm64` is Apple Silicon; `x86_64` is Intel.

On Intel there is nothing to think about. On Apple Silicon you are running an
arm64 stack where ADR 0011 specified x86-64; ADR 0021 amends that clause, and
[HOSTING.md](HOSTING.md) has the analysis behind it — every image in the stack
publishes a `linux/arm64` manifest and nothing in this repository pins a
platform.

**Read the caveat there as applying here.** That is evidence that nothing
structural stops an ARM build. It is not a build that has been run. Expect to
find something on the first `make deploy`, and if you do, amend ADR 0021 with
what it was.

## 4. Configuring

DEPLOYMENT.md's "Configuring" section and its table are correct as written.
Four adjustments:

**The checkout is yours, not `/opt/atp` owned by root.** Nothing in the stack
cares where it lives. Keep `chmod 600 .env` — it holds broker credentials and a
session-signing key, and file modes are the only thing protecting it on a
multi-user Mac.

**`ATP_WEB_BIND_ADDR` is a decision, not a copy of the table's answer.** The
table says "the `tailscale ip -4` address" because ADR 0011 chose Tailscale.
Locally you have three options and the default is the safest of them — §5.

**`sops` and `age` are Homebrew installs**, not the `linux.amd64` binary
DEPLOYMENT.md fetches:

```bash
brew install sops age
```

The bundle is also *optional* here in a way it is not on a remote host. Its
purpose in ADR 0011 is getting secrets onto a machine you are not sitting at;
you are sitting at this one, and a hand-written `.env` is a legitimate choice.
What the bundle still buys is a backup of your configuration that is safe to
commit, and a way to stand up a second host without retyping anything — which
matters exactly when you replace this Mac with the Linux box ADR 0021 names as
the upgrade path.

**`ATP_BACKUP_DIR` stops being optional.** See §6.

## 5. Reaching it

Unchanged from [DASHBOARD.md](DASHBOARD.md), "Reaching it from another machine",
which has the full table. Restated here only because the local case makes the
first row far more attractive than it is on a remote host:

| | `ATP_WEB_BIND_ADDR` | Reach |
|---|---|---|
| **This Mac only** *(default)* | leave empty | `http://127.0.0.1:8080` in the browser on this machine. Nothing on the network at all |
| **Tailscale / VPN** | `tailscale ip -4` (100.x) | Your phone, from anywhere, encrypted |
| **LAN** | the address from `ifconfig` / System Settings → Network | Any device on the wifi, plain HTTP |

**On a remote host, "localhost only" means an SSH tunnel every time you want to
look at it.** Here it means opening a browser. If you do not specifically need
the dashboard on your phone, leave `ATP_WEB_BIND_ADDR` empty and you have given
up nothing.

If you do want it elsewhere, prefer Tailscale over the LAN. `tailscale serve`
additionally fronts it with a real HTTPS certificate, which is what
`src/api/origin.ts`'s scheme derivation is waiting for, and gates on Tailscale
identity. Over plain HTTP on a LAN, the sign-in password and the whole book
cross the wire in clear text.

Two things that do **not** transfer from the Linux docs:

- **The `ufw` warning is Linux-specific.** Docker on Linux writes rules that are
  traversed before ufw's chain, so `ufw deny 8080` blocks nothing. macOS has no
  ufw and Docker Desktop publishes ports through a userspace process instead.
  The conclusion is unchanged and is the only part worth remembering: **the bind
  address is the control.** Do not set a wildcard and expect the macOS firewall
  to undo it.
- `make check-bindings` still refuses `0.0.0.0` and any publicly routable
  address, and still runs before `make deploy`. Nothing about being on a Mac
  relaxes it. Your machine's address comes from `ifconfig` or `tailscale ip -4`
  — not from searching "what is my IP", which returns your router's public
  address.

## 6. Backups, with no provider behind you

DEPLOYMENT.md says provider snapshots are the entire recovery story until
backups exist. **There is no provider here**, so that sentence has no local
equivalent and [BACKUPS.md](BACKUPS.md) is load-bearing from the first day.

```bash
make backup via=compose
make backup-verify via=compose
```

The first takes the dump. The second restores it into a scratch database and
compares it, which is the half that makes it a backup rather than a belief.

`via=compose` runs the client tools inside the `db` container. Use it: it avoids
needing a matching `pg_dump` on the Mac, and the binaries are then the server's
own build.

**Set `ATP_BACKUP_DIR` to somewhere that is not this machine's disk** — an
external volume, a synced folder, anything that survives the Mac not surviving.
A dump beside the database it came from dies with it, which BACKUPS.md says in
its first paragraph rather than its last.

**Time Machine is not a substitute.** A file-level copy of a running Postgres
volume is a copy of a database mid-write. The dumps are the backup; Time Machine
is how the dumps get off the machine, if that is the route you take.

And the thing to know before you need it, unchanged from DEPLOYMENT.md and
repeated because it inverts everything else here: a restored stack comes back
with an **empty** Redis, and an empty Redis holds no halt. The kill switch fails
closed against an *unreachable* Redis, not an empty one — so a restore starts
**willing to trade**, against a book as of the dump and a broker as of now.
`halt.py engage` first, every time.

### On a schedule, with launchd

[BACKUPS.md](BACKUPS.md) gives the cron lines. macOS does not use cron for this,
and the differences are not cosmetic.

**A LaunchAgent, never a LaunchDaemon.** The backup needs three things that only
exist inside a logged-in user's GUI session: Docker Desktop is running there, an
external drive is mounted into `/Volumes` by that session, and `uv` is on that
user's PATH. A LaunchDaemon runs as root at boot with none of the three, and
fails every time in a way whose log says only that the command was not found.

**launchd catches up a missed run; cron does not.** A `StartCalendarInterval`
job whose time passes while the Mac is asleep is run when it wakes, rather than
skipped. That is the single biggest reason this is workable on a laptop at all —
though a Mac that was fully powered off runs it at next login instead, which is
later than you think if you are away for a week.

#### The wrapper

Host-specific, so it lives on the machine and not in this repository — the same
reason `BACKUPS.md` keeps vendors out of the tooling. Substitute your own volume
name.

```bash
mkdir -p ~/bin && cat > ~/bin/atp-backup <<'EOF'
#!/bin/bash
set -euo pipefail

# launchd hands a job a near-empty PATH. Set it here as well as in the plist so
# the script is correct however it is invoked.
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

VOL="/Volumes/backup1"
DEST="$VOL/atp-backups"

# The volume must be a REAL mount. `backup_db.py create` calls
# `mkdir(parents=True)`, so against an unmounted drive it would cheerfully
# create /Volumes/<name>/ on the BOOT disk — putting the backup on the disk it
# is insuring against, and leaving a directory that stops the real drive
# mounting under that name afterwards.
if ! mount | grep -q " on $VOL "; then
  echo "REFUSING: $VOL is not mounted"; exit 1
fi
[ -w "$VOL" ] || { echo "REFUSING: $VOL is read-only"; exit 1; }

mkdir -p "$DEST"
cd "$HOME/algo-trading"

# One script, two modes, so the guard above cannot drift between a daily job
# and a weekly one. --dir explicitly, never ATP_BACKUP_DIR: that variable is
# read from the process environment only, never from .env, so it is absent
# under launchd and the dump would silently land in the repo.
case "${1:-create}" in
  create) exec uv run python scripts/backup_db.py create \
            --exec compose --dir "$DEST" --prune --keep 14 ;;
  verify) exec uv run python scripts/backup_db.py verify \
            --exec compose --dir "$DEST" ;;
  *)      echo "usage: atp-backup [create|verify]" >&2; exit 2 ;;
esac
EOF
chmod +x ~/bin/atp-backup
```

`atp-backup` takes the daily dump; `atp-backup verify` does the weekly restore
check. Two modes rather than two files, because two copies of that mount guard
is one copy that will eventually be wrong.

**Do not pass `--keep` to `verify` thinking it means retention.** On `create` it is an integer retention
count; on `verify` it is a flag meaning *leave the scratch database behind*. The
same word, two subcommands, opposite kinds of thing — and the wrong one quietly
accumulates `atp_restore_check_*` databases on your server.

#### The agents

`~/Library/LaunchAgents/local.atp.backup.plist`. **`~` does not expand in a
plist** — every path must be absolute, and `/Users/YOU` below is a substitution
you have to make.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.atp.backup</string>

  <key>ProgramArguments</key>
  <array><string>/Users/YOU/bin/atp-backup</string></array>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/Users/YOU/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>

  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>

  <key>RunAtLoad</key><false/>

  <key>StandardOutPath</key><string>/Users/YOU/Library/Logs/atp-backup.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/Library/Logs/atp-backup.log</string>
</dict>
</plist>
```

The weekly verify is the same file with `Label` `local.atp.backup-verify`, a
`ProgramArguments` array of `["/Users/YOU/bin/atp-backup", "verify"]`, and a
`Weekday` added to the interval (`0` is Sunday).

**Pick the hour against the market, not the clock.** The US session is
09:30–16:00 in New York; convert it to the host's local time and schedule
outside it. Prefer an hour the machine is *awake and the drive is attached* over
one that is merely quiet — a laptop shut at 03:00 defers the job to whenever it
next wakes, which is not a schedule.

#### Loading and — the part people skip — testing it

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.atp.backup.plist
launchctl print gui/$(id -u)/local.atp.backup | head -20

# Run it NOW rather than waiting a day to find out about a PATH mistake:
launchctl kickstart -k gui/$(id -u)/local.atp.backup
tail -20 ~/Library/Logs/atp-backup.log
```

To remove or replace one: `launchctl bootout gui/$(id -u)/local.atp.backup`.

Test with the drive **unplugged** as well. The wrapper should refuse and say so
in the log, rather than writing to the boot disk — that is the whole reason it
exists, and an untested guard is not a guard.

#### What this still does not give you

**Nothing shouts when it fails.** launchd writes the exit code to its log and
takes no further interest. `BACKUPS.md` makes the same point about its cron
line, and it is sharper here: an agent that has been failing since the last OS
update looks exactly like one that has been succeeding, until you open the log.
Wiring the exit code to `scripts/check_alerts.py` is the gap to close before a
paper week, not after it.

## After the first deploy

DEPLOYMENT.md's "After every deploy" list applies unchanged, except that the
Tailscale line only applies if you chose that route. The local equivalent:

```bash
uv run python scripts/status.py
docker compose ps
curl -sf http://127.0.0.1:8000/healthz
curl -sf http://127.0.0.1:8080/healthz
python3 scripts/check_port_bindings.py
date -u
docker compose exec -T api date -u
```

In order: halts, quote freshness and latest bars; every service up and none
restarting; the API on loopback; the same through nginx; nothing exposed and the
deployed shape intact; and the host and VM clocks agreeing.

Then the two verifications that are specific to this host and that nothing in
the repository can do for you:

1. **Restart the Mac** and confirm the whole stack comes back on its own,
   without you starting Docker Desktop. DEPLOYMENT.md asks for the same test on
   a VM, "on a day that does not matter"; here it is also testing the login-item
   setting from §2.
2. **Leave it overnight** and check `pmset -g log` for sleep events and
   `scripts/status.py` for a gap. This is the one that decides whether §1 is
   handled or merely intended.

## What this does not give you

Everything in DEPLOYMENT.md's "What this does not give you" still holds. Three
additions specific to hosting here:

- **US-East proximity.** Accepted deliberately in ADR 0021. A daily-bar strategy
  will not notice; anything reacting within the bar will. The dashboard is
  unaffected — it is read on demand (ADR 0022), so your distance from it costs
  one round trip when you ask for one.
- **A second host for live.** `docs/SAFETY.md` layer 3 wants paper and live on
  separate machines with separate key pairs, and ADR 0021 chose a paper host
  only. Going live is a decision to make after the paper week, not before it.
- **Somewhere to go when this stops being enough.** ADR 0021 names a dedicated
  Linux box as the upgrade path and HOSTING.md costs the free alternative;
  [ORACLE_HOSTING.md](ORACLE_HOSTING.md) is the procedure for that alternative,
  including the cutover from this machine while it is running.
- **Isolation from your own use of the machine.** This is also the computer you
  work on. A reboot for an unrelated reason is now a deploy-time event, and
  `docs/SAFETY.md` rule 4 — never in the last thirty minutes of a session —
  applies to reasons that have nothing to do with trading.
