# tinderbox-ng

Gentoo build-testing rig with overlayfs sessions. Builds an **immutable
baseline** (latest stage3 + SWI-Prolog + [portage-ng](https://github.com/pvdabeel/portage-ng)
+ matching `kb.qlf`) and exposes it as the lower layer of an overlayfs-backed
`chroot`. Each experiment runs in its own session whose writes land in an
upper layer that can be discarded with one command.

The host OS root is never touched: the baseline lives entirely under
`$TINDERBOX_ROOT` (default `/srv/tinderbox-ng`), the Portage tree binds
in read-only, and host-side caches like `/var/db/pkg` are not mounted in.

`tinderbox-ng` was extracted from the
[`portage-ng`](https://github.com/pvdabeel/portage-ng) repository at
commit `26069d06` and now lives standalone. The
[Compatibility with portage-ng](#compatibility-with-portage-ng) section
pins the cross-repo contract.

## Layout

```text
tinderbox-ng/
├── README.md                          # this file
├── LICENSE
├── bin/
│   ├── tinderbox-ng                   # main script
│   └── deploy-host.sh                 # one-shot host install (rsync + symlink + doctor)
├── share/tinderbox-ng/                # everything cp_template / install touches at bootstrap
│   ├── baseline.make.conf             # /etc/portage/make.conf for baseline
│   ├── baseline.package.use           # /etc/portage/package.use/00-tinderbox-ng-defaults
│   ├── baseline.repos.conf            # /etc/portage/repos.conf/gentoo.conf
│   ├── portage-ng-dev.in              # in-chroot launcher (template)
│   ├── deploy-baseline.sh             # safe scp of templates into a live baseline
│   ├── run-matrix.sh                  # tinderbox-matrix test runner
│   ├── compare-matrix.sh              # parallel `compare` driver over a manifest
│   ├── easy-pkgs.sh                   # convenience driver over a curated easy-pkg list
│   ├── manifest-100.txt               # smoke manifest (100 atoms)
│   └── manifest-1000.txt              # full matrix manifest (1000 atoms)
├── tools/
│   └── render-compare-matrix.py       # render compare-matrix TSVs to Markdown
└── reports/                           # historical compare-matrix runs (snapshots)
```

After `bootstrap`, the rig populates the VM as:

```text
/srv/tinderbox-ng/
├── baseline/                          # lower layer; frozen after build
├── shared/
│   ├── portage-tree/                  # git-pinned Portage tree (ro into sessions)
│   ├── portage-tree.commit            # current pinned commit hash
│   ├── distfiles/                     # persistent fetch cache
│   ├── ccache/                        # optional
│   ├── binpkgs/                       # optional shared binpkgs
│   └── stage3/                        # downloaded stage3 tarballs + signatures
├── sessions/
│   └── <name>/{upper,work,merged,logs,info,.lock}
├── scripts/
└── logs/
```

## Deployment on the VM

The script depends only on Bash 5+, `mount(8)`, `umount(8)`, `findmnt(8)`,
`flock(1)`, `gpg(1)`, `curl(1)`, `git(1)`, `tar(1)`, `rsync(1)`, the
in-kernel `overlay` module, and (for the test matrix) `app-admin/moreutils`
for `ts(1)` (optional — falls back to plain `tee`).

`tinderbox-ng doctor` aggregates every prerequisite and reports all problems
in one pass; `bootstrap` runs it implicitly so a missing tool surfaces *before*
stage3 download.

### Recommended: `deploy-host.sh` (one-shot)

```sh
# From your dev machine - first-time install on a fresh VM (long: ~hours):
TINDERBOX_BOOTSTRAP_SELFTEST=1 \
  bin/deploy-host.sh --bootstrap root@vm-linux.local

# After `git pull` on the dev machine - refresh script + templates only:
bin/deploy-host.sh root@vm-linux.local

# Refresh script + templates AND push your latest portage-ng src into the
# already-bootstrapped baseline (does NOT regenerate kb.qlf):
bin/deploy-host.sh --refresh-portage-ng root@vm-linux.local
```

`deploy-host.sh` performs, in order:

1. `rsync` of the entire repo (`bin/`, `share/`, `tools/`; excludes
   `.git/`, `reports/`, `bin/deploy-host.sh`) to
   `/usr/local/share/tinderbox-ng/` on the remote.
2. Symlink `/usr/local/sbin/tinderbox-ng` →
   `/usr/local/share/tinderbox-ng/bin/tinderbox-ng`.
3. `tinderbox-ng doctor` on the remote (preflight checks; fails fast on
   missing prerequisites before any heavy work starts).
4. Optional: `tinderbox-ng bootstrap` (with `--bootstrap`).
5. Optional: `tinderbox-ng selftest` (with `--selftest` or
   `TINDERBOX_BOOTSTRAP_SELFTEST=1` during a `--bootstrap` run).

Forwarded environment: `TINDERBOX_CCACHE_MAX_SIZE`,
`TINDERBOX_SESSIONS_TMPFS_SIZE`, `TINDERBOX_REBOOTSTRAP`, `STAGE3_VARIANT`,
`STAGE3_ARCH`, `PORTAGE_TREE_PIN`, `GENTOO_PROFILE`,
`PORTAGE_NG_URL`, `PORTAGE_NG_LOCAL`, `PORTAGE_NG_REF`, etc. — see
`bin/deploy-host.sh --help` for the full list.

### Manual install (equivalent steps)

```sh
# From your dev machine, push the rig to the VM:
rsync -av --delete --exclude '/.git' --exclude '/reports' \
  ./ vm-linux.local:/usr/local/share/tinderbox-ng/

# Symlink the entry point onto $PATH:
ssh vm-linux.local sudo ln -sf \
  /usr/local/share/tinderbox-ng/bin/tinderbox-ng \
  /usr/local/sbin/tinderbox-ng

# Confirm prerequisites:
ssh vm-linux.local sudo tinderbox-ng doctor
```

The script auto-detects `LIB_DIR`: it prefers `share/tinderbox-ng/` next
to its own `bin/` (works for a dev checkout and the recommended installed
layout), then falls back to `/usr/local/share/tinderbox-ng/share/tinderbox-ng`
and finally to a flattened `/usr/local/share/tinderbox-ng/`. Override with
`TINDERBOX_LIB_DIR`.

## Lifecycle

### One-shot bootstrap

```sh
# On the VM:
sudo tinderbox-ng bootstrap

# Or, with a post-bootstrap smoke test:
sudo TINDERBOX_BOOTSTRAP_SELFTEST=1 tinderbox-ng bootstrap
```

This step is **long-running** (hours). It begins with `tinderbox-ng doctor`
(skip with `TINDERBOX_SKIP_DOCTOR=1` only on machines you've already
qualified) so any missing host-side tool is reported up front:

1. Resolves the latest stage3 from `latest-stage3-amd64-openrc.txt`.
2. Verifies `.DIGESTS` against the Gentoo release-engineering GPG key
   (`0xBB572E0E2D182910`) and the SHA512 inside.
3. Unpacks into `baseline/` with `--xattrs-include='*.*' --numeric-owner`.
4. Clones the Portage tree into `shared/portage-tree/` and pins it
   (commit hash recorded at `shared/portage-tree.commit`).
5. Writes `/etc/portage/make.conf` and `/etc/portage/repos.conf/gentoo.conf`
   from the templates in `share/tinderbox-ng/`.
6. Runs `eselect profile set default/linux/amd64/23.0/split-usr/no-multilib`
   (matches `config:gentoo_profile/1` in `Source/config.pl`),
   `locale-gen`, `gcc-config -l`, `binutils-config -l`,
   `ln -sf /proc/self/mounts /etc/mtab`.
7. `emerge dev-lang/swi-prolog dev-vcs/git net-misc/curl`.
8. `emerge dev-util/ccache` and writes `/etc/ccache.conf` with
   `max_size = $TINDERBOX_CCACHE_MAX_SIZE` (default 100G), then
   `chown portage:portage /var/cache/ccache && chmod 2775` so the
   shared cache is writable by Portage's `userfetch`/`usersandbox` user.
   Stage3 does not include ccache; without this step the
   `FEATURES="ccache"` bit is silently inert.
9. Installs `portage-ng` into `/opt/portage-ng` from
   `$PORTAGE_NG_LOCAL` (rsync) or `$PORTAGE_NG_URL` (git clone).
   At least one of the two must be set, or one of the conventional deploy
   paths (`/root/prolog`, `/opt/portage-ng`, `/opt/prolog`) must already
   exist on the host. `doctor` reports a missing setting as a hard error
   before anything is downloaded.
10. Installs the in-chroot `portage-ng-dev` launcher at
   `/usr/local/bin/portage-ng-dev` and `tinderbox-matrix` runner at
   `/usr/local/bin/tinderbox-matrix`.
11. Runs `portage-ng-dev --mode standalone --sync` once with the tree
    bound *rw* (the only time this happens) to generate `kb.qlf` from
    the pinned tree, then re-pins.
12. Freezes the baseline with `chmod -R a-w`. **Note:** this is a soft
    freeze - root can still write (DAC bypass). We deliberately do **not**
    use `chattr +i` because the immutable flag on lower-layer files
    propagates `EPERM` through overlayfs's copy-up path, which makes
    sessions read-only. The chmod is a speed bump against accidental
    `cp ... /srv/tinderbox-ng/baseline/...`, not a hard barrier.
13. Optionally runs `tinderbox-ng selftest` (when
    `TINDERBOX_BOOTSTRAP_SELFTEST=1`): a `compare --pretend` of
    `sys-apps/portage` (overrideable via `TINDERBOX_SELFTEST_TARGET`) in a
    throwaway session. Catches "bootstrap finished cleanly but kb.qlf is
    broken" or "the baseline missed a config flag" regressions in seconds.

### Per-session work

```sh
sudo tinderbox-ng new toolchain-stress
sudo tinderbox-ng enter toolchain-stress     # interactive chroot
# ... or non-interactively:
sudo tinderbox-ng exec toolchain-stress -- \
  tinderbox-matrix resolver /tmp/manifest.txt
sudo tinderbox-ng diff toolchain-stress      # what files changed
sudo tinderbox-ng reset toolchain-stress     # discard upper, keep session
sudo tinderbox-ng destroy toolchain-stress   # remove session entirely
sudo tinderbox-ng list                       # status of all sessions
```

The mount stack (overlay + fresh `devtmpfs` for `/dev` + `devpts` for
`/dev/pts` + `tmpfs` for `/dev/shm` + `proc` + `sysfs` + `tmpfs /run`
+ `ro` Portage tree + `rw` distfiles + optional ccache/binpkgs +
`/etc/resolv.conf` bind) is defined in `_ns_session_mount()` inside the
script. It always runs inside an unshared mount namespace (entered via
the internal `__ns-helper` re-exec). There is no explicit teardown - when
the chroot's shell exits, the namespace is destroyed and every mount
inside it disappears with it.

The `exec`, `compare`, `reset`, `destroy` and `exit` subcommands
additionally call an orphan-reaper that scans `/proc/*/root` for any
processes still anchored under `$SESSIONS_DIR/$name/` and SIGKILLs them.
This catches portage `die_hooks` chains (`sandbox` → `misc-functions.sh
die_hooks` → `ebuild-ipc.py exit 0`) that survive past the parent
emerge's death and otherwise pin a CPU at ~90% indefinitely.

### Refreshing the baseline

```sh
sudo tinderbox-ng refresh-tree <commit-hash>          # re-pin the tree
sudo tinderbox-ng refresh-kb                          # regenerate kb.qlf
sudo tinderbox-ng refresh-portage-ng                  # rsync new portage-ng source into baseline
sudo TINDERBOX_CCACHE_MAX_SIZE=200G \
     tinderbox-ng install-ccache                      # bump cache cap or retrofit
```

`refresh-tree` only updates `shared/portage-tree/`. Existing sessions
keep their old bind until you `reset` them (this is intentional — you
do not want a long-running test matrix to suddenly see a different tree
mid-run). `refresh-kb` temporarily unfreezes the baseline (`chmod -R u+w`,
plus `chattr -R -i` defensively in case an old freeze used it), runs
`portage-ng-dev --sync` against the pinned tree, then re-freezes.

`refresh-portage-ng` re-deploys the Prolog source tree itself (mirrors the
`bootstrap_install_portage_ng` step). Use it after pulling new commits to
the host-side checkout: every fresh session bind-mounts the baseline copy,
so without this step new sessions keep running yesterday's resolver. It
unfreezes, rsyncs (or `git fetch`/`reset --hard` if `PORTAGE_NG_URL` is
set), refreshes the in-baseline `/usr/local/bin/portage-ng-dev` shim plus
the `tinderbox-matrix` helper, then re-freezes. It does **not** regenerate
`kb.qlf` — only run `refresh-kb` for that, and only when the parser or
grammar actually changed (planner / pipeline / scheduler / printer edits
do not require it).

`install-ccache` is the same `bootstrap_install_ccache` step run
standalone: useful to retrofit ccache into a baseline that was
bootstrapped on an older revision of `tinderbox-ng`, or to bump
`max_size` in `/etc/ccache.conf` mid-stream by setting
`TINDERBOX_CCACHE_MAX_SIZE` and re-running. It unfreezes the baseline
briefly, re-emerges `dev-util/ccache` (a no-op if already current),
rewrites `/etc/ccache.conf`, and re-freezes.

## portage-ng integration

Verified facts from the [`portage-ng`](https://github.com/pvdabeel/portage-ng)
checkout (against the pinned commit `26069d06`):

- `Source/Config/vm-linux.local.pl` registers `portage` at
  `/usr/portage`, `pkg` at `/var/db/pkg`, `distfiles` at
  `/var/cache/distfiles`, and the binpkg cache at
  `/srv/tinderbox-ng/shared/binpkgs`. These match the in-chroot mount
  points used by `tinderbox-ng` exactly.
- `bootstrap` and `refresh-portage-ng` both append a tinderbox-ng-owned
  override block to that host config that flips portage-ng's default
  `config:binpkg_refresh(manual)` to `mtime` (declaring the predicate
  dynamic and retracting the upstream fact first, so the override
  survives any once/1 or first-fact-wins consumer). Without `mtime`,
  long-running `compare-matrix --jobs N` workers never see binpkgs
  their siblings just produced via `FEATURES=buildpkg`, so the matrix
  tail re-builds packages from source even though they're sitting in
  the shared `Packages` index. See `_patch_portage_ng_host_config` in
  `bin/tinderbox-ng`. The override is idempotent: if the host config
  already declares `config:binpkg_refresh/1` (e.g. an operator
  deliberately set `manual`), tinderbox-ng leaves it alone.

  Caveat (as of pinned commit `26069d06`): portage-ng documents the
  `manual|mtime` contract in `Source/config.pl` and
  `Source/Domain/Gentoo/Binpkg/binpkg_exec.pl` pldoc, but
  `binpkg_exec:available_for/4` does **not** yet consult
  `config:binpkg_refresh/1` — the dispatch is still TODO upstream. The
  override is the correct policy for the day the consumer lands; until
  then it is a harmless no-op and the matrix continues to see only the
  binpkgs that existed at session start. The fix for that symptom
  belongs in `pvdabeel/portage-ng`, not here.
- `Source/config.pl` pins
  `config:pkg_directory('vm-linux.local','/var/db/pkg')` — the chroot's
  own VDB, which lives in the session's upper layer.
- `Source/config.pl` pins
  `config:graph_directory('vm-linux.local','/root/Graph')` — `.merge`
  files land in `/root/Graph/portage/` inside the chroot. The bootstrap
  pre-creates that directory.
- The world file lives at `Source/Knowledge/Sets/world/vm-linux.local`.
  The bootstrap creates an empty file there so `--pretend` runs do not
  crash on first read; any writes happen in the session upper layer and
  are wiped by `reset`.
- `chroot(8)` does not enter a new UTS namespace, so
  `socket:gethostname/1` returns `vm-linux.local` inside the chroot too.
  The script does **not** use `unshare -u`.

**Do not run `portage-ng` on the VM host.** The host's `/var/db/pkg` is
the real production VDB; the existing `vm-linux.local.pl` would point
at it. Always run `portage-ng-dev` from inside a `tinderbox-ng` session.

## Compatibility with portage-ng

`tinderbox-ng` is the only consumer of a small contract surface that
`portage-ng` exposes via its `--mode standalone` CLI. Until further
notice the contract documented here is the API; breaking it requires
coordinated changes to both repositories.

**Pinned commit known to work:**
[`pvdabeel/portage-ng@26069d06`](https://github.com/pvdabeel/portage-ng/tree/26069d06)
— anything from this commit forward should work as long as the items
below stay stable. If you bump the on-host portage-ng checkout past a
known-incompatible commit and bootstrap fails, pin
`PORTAGE_NG_REF=26069d06` (when using `PORTAGE_NG_URL`) until the
contract is restored.

### CLI surface

`portage-ng-dev --mode standalone` accepts the following flags and any
combination of them in the cases tinderbox-ng exercises:

| Flag | Used by | Semantic |
|------|---------|----------|
| `--ci` | every in-chroot invocation | non-interactive; required for stable exit codes |
| `--sync` | bootstrap, `refresh-kb` | populates `Knowledge/kb.qlf` (and `profile.qlf`) |
| `--pretend` | `cmd_compare`, `cmd_portage_ng`, `tinderbox-matrix` | plan only |
| `--build` | `cmd_compare`, `cmd_portage_ng`, `easy-pkgs.sh` | execute the plan |
| `--timeout N` | (optional, only when wrapper template forwards it) | per-invocation watchdog |

### Exit-code triage

```
0 = no assumptions
1 = only prover cycle-break assumptions
2 = at least one domain assumption (e.g. masked dep)
3 = invalid targets (no resolvable atom passed)
* = crash (anything else)
```

`compare-matrix.sh` and `_compare_summarize` interpret 0/1/2 as
"plan produced", 3+ as failure. In particular, `Source/Application/Interface/Action/build.pl`
in `portage-ng` deliberately returns `halt(3)` on invalid-target failures
specifically so this triage is unambiguous; if you change exit-code
semantics in `portage-ng` without updating tinderbox-ng, every compare
result silently misclassifies.

### Output formats

- The planner footer must contain `Total: <N> action[s]`.
- The build summary must contain `Total: <N> completed`.
- ANSI escape sequences around the numbers are tolerated (tinderbox-ng
  strips them with `sed -E 's/\x1b\[[0-9;]*[a-zA-Z]//g'`).
- `--sync` postcondition: `Knowledge/kb.qlf` exists at the portage-ng
  repo root after the run.

### Filesystem layout (portage-ng repo root)

`tinderbox-ng` rsyncs the on-host `portage-ng` checkout into the
baseline at `/opt/portage-ng/`. The wrapper requires the following at
the repo root:

- `portage-ng.pl` — project entry point (used as `swipl -f`).
- `Source/loader.pl` — module loader (alternative repo marker).
- `Source/Config/<hostname>.pl` — host-specific config; hostname inside
  the chroot is `vm-linux.local`.
- `Source/Knowledge/Sets/world/<hostname>` — world file (created empty
  by bootstrap if missing).

The following gitignored paths must NOT be deleted by `refresh-portage-ng`
(they are produced inside the baseline by `--sync`/runtime and are not
in the source repo):

- `Knowledge/{kb.qlf, kb.raw, profile.qlf, profile.raw, embeddings.pl,
  phase_stats.pl, resume.pl}`
- `Source/{Snapshots, Certificates, Private}/`
- `Source/Knowledge/Sets/**/*.local`

The rsync exclude list in `bin/tinderbox-ng` (function
`cmd_refresh_portage_ng`) is the canonical encoding of this rule.

## Side-by-side comparison: portage-ng vs emerge

`tinderbox-ng` ships a comparison harness that runs the same target through
both engines in **separate, identical, fresh sessions** and prints a table
of their differences. Use it instead of guessing whether one engine
"would" succeed or fail.

```sh
# Default: --pretend (planner-only), both sessions destroyed at the end
tinderbox-ng compare www-servers/apache

# Actually run the build phases on both sides
tinderbox-ng compare --build www-servers/apache

# Keep the sessions afterwards so you can inspect VDB / file system
tinderbox-ng compare --build --keep --label apache-debug www-servers/apache

# Convenience wrappers (single-engine, when you just want to drive one):
tinderbox-ng portage-ng <session> [--pretend|--build] <pkg>...
tinderbox-ng emerge     <session> [--pretend]        <pkg>...
```

The summary table contrasts:

- **exit**: 0/non-zero from each engine
- **plan actions**: number reported by each planner (portage-ng: actions,
  emerge: packages)
- **completed**: number of completion markers (portage-ng: `Total: N
  completed`, emerge: `>>> Completed`)
- **merged into VDB**: `cat/name-version` directories that ended up in the
  session's upper VDB (`var/db/pkg`)
- **VDB delta**: which packages only one side merged

Logs land in `/srv/tinderbox-ng/logs/compare-<label>-<stamp>/` as
`portage-ng.log` and `emerge.log`. With `--keep`, the upper layer of
each session is preserved at
`/srv/tinderbox-ng/sessions/pkg-cmp-{portage-ng,emerge}-<label>/upper/`.

Exit code: `0` if both succeeded, `1` if exactly one failed, `2` if both
failed. Useful for CI sweeps.

## Test matrix

`tinderbox-matrix` is installed at `/usr/local/bin/tinderbox-matrix` in
the baseline. Manifest format is one atom per line, `#` for comments:

```text
# manifest.txt
sys-apps/portage
dev-lang/swi-prolog
dev-libs/glib
=app-editors/neovim-0.10.2
```

Tiers (cheapest → most expensive):

| Tier | What it does |
|------|--------------|
| `metadata` | `pkgcheck scan` per atom (requires `dev-util/pkgcheck`). |
| `resolver` | `emerge -vp` + `portage-ng-dev --pretend` per atom; captures the worse of the two exit codes. |
| `merge` | `emerge --jobs=N --keep-going=y` over the whole manifest. |
| `test` | `FEATURES=test emerge --oneshot` per atom, with `timeout` (`TIMEOUT_DEFAULT=1800`). |
| `emptytree` | `emerge --emptytree --pretend @world` (manifest ignored). |

Each tier writes per-atom logs and an aggregated TSV summary to
`/var/log/tinderbox-matrix/<tier>/`. From the host:

```sh
ssh vm-linux.local sudo cat \
  /srv/tinderbox-ng/sessions/toolchain-stress/merged/var/log/tinderbox-matrix/resolver/summary.tsv
```

(Or grab the whole log dir at session-end via `tinderbox-ng exec ... -- tar -C /var/log -czf - tinderbox-matrix`.)

### Live monitoring: `tinderbox-ng progress`

```sh
sudo tinderbox-ng progress              # interactive dashboard, refresh 2s
sudo tinderbox-ng progress --interval 5 # slower refresh
sudo tinderbox-ng progress --once       # one-shot dump (good for ssh/cron)
sudo tinderbox-ng progress --run /srv/tinderbox-ng/reports/compare-matrix-...
                                         # pin to a specific run instead of
                                         # autodetecting the latest one
```

The dashboard auto-detects the most recent `compare-matrix-*` run under
`$TINDERBOX_ROOT/reports/` (preferring an unfinished one) and continuously
refreshes:

- **Progress bar + ETA**: bar filled to `done / total`, packages-per-hour
  rate, average seconds per package, projected wall-clock finish time.
- **Host load**: 1m/5m/15m loadavg coloured against `nproc` (green if
  `< nproc`, yellow if over, red if `> 1.5 × nproc`); RAM used/total;
  sessions-tmpfs occupancy; distfiles + ccache disk usage.
- **Active sessions**: each session in `$TINDERBOX_ROOT/sessions/` with
  its inferred engine (`portage-ng` / `emerge` / `compare` / `selftest`
  / `user`), mount state (MOUNTED / idle), and age since creation.
- **Recent completions**: tail of `results.tsv` showing the last five
  packages with PN/EM exit status, VDB delta, and seconds.

Uses bash's alternate-screen buffer so your terminal scrollback survives;
press `q` (or `^C`) to exit. When stdout is not a tty (e.g. piped through
ssh), automatically falls back to single-frame `--once` mode so you can
script ad-hoc snapshots:

```sh
ssh root@vm-linux.local tinderbox-ng progress --once
```

## Safety guarantees

- **Read-only Portage tree in sessions.** `mount_session` does the
  two-step `bind` + `remount,bind,ro`; `findmnt` re-checks the mount is
  `ro` and refuses to chroot if it isn't. The only code paths that
  bind the tree `rw` are `bootstrap` and `refresh-kb`.
- **No host caches mounted in.** `/var/db/pkg`, `/var/lib/portage`,
  `/var/cache/edb`, `/var/cache/eix`, `/var/log`, `/var/log/portage`,
  `/etc/portage` are **never** referenced.
- **Shared session caches (opt-in).** `_ns_session_mount` bind-mounts
  three host paths into every session if they exist:
  - `$SHARED_DIR/distfiles` → `/var/cache/distfiles` (always created)
  - `$SHARED_DIR/binpkgs`   → `/var/cache/binpkgs`   (created when `buildpkg` is in FEATURES)
  - `$SHARED_DIR/ccache`    → `/var/cache/ccache`    (created by bootstrap, populated by builds)

  The ccache wiring requires three things in lockstep — *any one missing
  silently disables it without a warning*:
  1. `/srv/tinderbox-ng/shared/ccache/` exists on the host (bind source).
     Bootstrap creates it; `_ns_session_mount` only adds the bind if it exists.
  2. `dev-util/ccache` is installed in the baseline (provides the
     `/usr/lib/ccache/bin` compiler shims). **Stage3 does not include
     it.** `bootstrap_install_ccache` emerges it as part of the standard
     bootstrap pipeline; for older baselines, run
     `sudo tinderbox-ng install-ccache` to retrofit.
  3. `FEATURES` contains `ccache` in `baseline.make.conf`. The shipped
     template enables it; verify with `emerge --info | grep FEATURES`.

  The cache is sized via `TINDERBOX_CCACHE_MAX_SIZE` (default 100G,
  written into `/etc/ccache.conf` at install time). Bump it via
  `sudo TINDERBOX_CCACHE_MAX_SIZE=200G tinderbox-ng install-ccache`,
  which rewrites the config in place and re-freezes the baseline.
- **Soft-frozen baseline.** Bootstrap finishes with `chmod -R a-w`.
  This is a speed bump against accidental host-side edits; root can
  still write (DAC bypass). We do not use `chattr +i` because it would
  break overlayfs copy-up - sessions need to write to files that exist
  in the lower layer, and the immutable flag on lower files propagates
  `EPERM` to copy-up, freezing the entire chroot.
- **Per-build parallelism.** `baseline.make.conf` ships `MAKEOPTS="-j@NPROC@ -l@NPROC@"`
  and `EMERGE_DEFAULT_OPTS="--jobs=@NPROC@ ..."`. `cp_template` rewrites
  `@NPROC@` to the host's `nproc` value at bootstrap time. **Do not use
  literal `$(nproc)` in `make.conf`** — Portage's parser does not expand
  command substitution. **And do not raw-`scp` a template into a live
  baseline either** — that bypasses the `cp_template` substitution and
  leaves literal `@NPROC@` placeholders in `make.conf`, crashing emerge
  with `Invalid --jobs parameter: '@NPROC@'`. Use
  `share/tinderbox-ng/deploy-baseline.sh <template> <user@host:remote-path>`
  to push a template into a running baseline; it applies the same
  substitution as `cp_template` and refuses to deploy if any `@TOKEN@`
  is left unresolved. If you discover a baseline whose `make.conf` still
  contains literal `@NPROC@` (e.g. from a pre-`deploy-baseline.sh` raw
  `scp`), patch it in place with:

  ```sh
  ssh root@vm-linux.local 'NPROC=$(nproc); chmod u+w /srv/tinderbox-ng/baseline/etc/portage/make.conf; \
    sed -i "s|@NPROC@|${NPROC}|g" /srv/tinderbox-ng/baseline/etc/portage/make.conf; \
    chmod a-w /srv/tinderbox-ng/baseline/etc/portage/make.conf'
  ```
- **Test phase opt-in (matches Portage).** `portage-ng-dev --build`
  honours `FEATURES="test"` from `make.conf` (or env). When `test` is
  not in `FEATURES` the `test` phase is omitted from the ebuild phase
  list entirely (`ebuild_exec:build_phases/1` consults
  `config:features_test_enabled/0`). The default `baseline.make.conf`
  does **not** enable `test`; many ebuilds (`vim`, `glibc`, etc.) have
  interactive or TTY-attached test suites that hang in non-interactive
  sessions. To exercise tests for a specific package, set per-package
  `FEATURES="test"` via `/etc/portage/package.env` inside the session.
- **Mount-namespace isolation.** Every `bootstrap`, `refresh-kb`,
  `enter`, and `exec` re-execs into `unshare --mount --propagation=private`
  before mounting anything. Mounts made inside that namespace are
  invisible to the host and the kernel reaps them automatically when
  the namespace exits. We never call `umount -l`, never `--rbind /dev`
  (each namespace gets a fresh `devtmpfs`), and never leave host-namespace
  mounts behind. This is the design enforced after the May 2026 host-/dev
  incident; see the in-script comments above `_ns_session_mount` and
  `_ns_baseline_mount` for the full rationale.
- **Session locks.** `tinderbox-ng enter|exec|reset|destroy` take an
  `flock` on `sessions/<name>/.lock`; concurrent invocations on the
  same session fail fast.
- **Propagation-safe teardown.** `mount --make-rslave` is applied to
  the rbind targets themselves, so `umount -R` cannot propagate to the
  host's `/dev` or `/sys`.

## kb.qlf consistency rule

`kb.qlf` is qcompiled from `kb.raw`, which reflects the Portage tree at
sync time. If the bind-mounted tree changes underneath a baseline that
ships a particular `kb.qlf`, the resolver disagrees with `emerge`.
`tinderbox-ng` enforces consistency by:

1. Pinning the tree via `git checkout <commit>`.
2. Recording the commit at `shared/portage-tree.commit`.
3. Binding the tree `ro` into every session.
4. Forbidding `emerge --sync` inside sessions (the `ro` bind makes it
   fail; the warning surfaces in the log).
5. Re-running `--sync` only via `refresh-kb`, which re-pins after.

## Out of scope (tracked)

- **Compare pipeline integration.** `portage-ng` ships
  `Reports/Scripts/generate-emerge-files.sh` and
  `Reports/Scripts/compare-merge-emerge.py` to compare `.merge` vs
  `.emerge` files. The shell driver hard-codes macOS prefix paths
  (`/Volumes/...`); a Linux/in-chroot port that calls `emerge -vp`
  directly is needed before the `.merge`/`.emerge` pipeline can run on
  the tinderbox VM. Both file types would then land in
  `/root/Graph/portage/` inside the session.
- **Optional `dev-util/pkgcore` cross-resolver** as a third opinion
  alongside `emerge` and `portage-ng-dev`.
- **A/B baselines** (e.g. multilib vs no-multilib in two parallel
  baselines under `/srv/tinderbox-ng-multilib/`).

## Environment reference

| Variable | Default | Purpose |
|----------|---------|---------|
| `TINDERBOX_ROOT` | `/srv/tinderbox-ng` | Root for everything. |
| `TINDERBOX_LIB_DIR` | next to script | Override template directory. |
| `STAGE3_VARIANT` | `amd64-openrc` | Stage3 flavor. |
| `STAGE3_ARCH` | `amd64` | Stage3 architecture (also flips the URL). |
| `STAGE3_BASE_URL` | `https://distfiles.gentoo.org/releases/<arch>/autobuilds` | Mirror. |
| `GENTOO_RELENG_KEY` | `0xBB572E0E2D182910` | Release-engineering key fingerprint. |
| `PORTAGE_TREE_URL` | `https://github.com/gentoo-mirror/gentoo.git` | Tree source. |
| `PORTAGE_TREE_PIN` | (latest) | Pin to a specific commit at bootstrap. |
| `PORTAGE_NG_URL` | (unset) | If set: `git clone`; else rsync from `PORTAGE_NG_LOCAL`. |
| `PORTAGE_NG_LOCAL` | one of `/root/prolog`, `/opt/portage-ng`, `/opt/prolog` if it looks like a portage-ng checkout, else unset | Local portage-ng checkout to seed `/opt/portage-ng`. Bootstrap fails if neither this nor `PORTAGE_NG_URL` resolves. |
| `PORTAGE_NG_REF` | `HEAD` | Ref to check out (only if `PORTAGE_NG_URL` set). Pin to `26069d06` for a known-compatible portage-ng. |
| `GENTOO_PROFILE` | `default/linux/amd64/23.0/split-usr/no-multilib` | Must match `config:gentoo_profile/1`. |
| `GENTOO_LOCALE` | `en_US.UTF-8 UTF-8` | Appended to `/etc/locale.gen`. |
| `GENTOO_LOCALE_NAME` | `en_US.utf8` | Argument to `eselect locale set`. |
| `TINDERBOX_SESSIONS_TMPFS_SIZE` | `100G` | tmpfs cap for `$TINDERBOX_ROOT/sessions`. Empty/`0` disables. |
| `TINDERBOX_CCACHE_MAX_SIZE` | `100G` | `max_size` written into `/etc/ccache.conf` by `bootstrap_install_ccache` and `install-ccache`. |
| `TINDERBOX_REBOOTSTRAP` | (unset) | If set, `bootstrap` overwrites an existing baseline. |
| `TINDERBOX_SKIP_DOCTOR` | (unset) | If set, `bootstrap` skips its preflight doctor pass. |
| `TINDERBOX_BOOTSTRAP_SELFTEST` | (unset) | If set, `bootstrap` runs `selftest` on completion. |
| `TINDERBOX_SELFTEST_TARGET` | `sys-apps/portage` | Atom used by `selftest`'s `compare --pretend`. |
| `TINDERBOX_MIN_FREE_MIB` | `30000` | Disk-space floor (MiB) `doctor` checks under `dirname $TINDERBOX_ROOT`. |
| `TINDERBOX_MDNS_HOSTS` | `mac-pro.local imac-pro.local` | mDNS hostnames `doctor` resolves and `_inject_mdns_hosts` writes into each session's `/etc/hosts`. |
