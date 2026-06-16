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

The repository follows FHS conventions: `bin/` for the on-VM CLI entry
point, `libexec/<name>/` for executable helpers `tinderbox-ng` invokes
internally, `share/<name>/` for read-only data (templates, manifests,
manpage), and `contrib/` for dev-machine helpers that never get pushed
to the VM.

```text
tinderbox-ng/
├── README.md                          # this file
├── LICENSE
│
├── bin/                               # On-VM CLI entry point
│   └── tinderbox-ng                   #   → /usr/local/sbin/tinderbox-ng (symlinked)
│
├── libexec/tinderbox-ng/              # Executable helpers tinderbox-ng calls (not user-facing)
│   ├── compare-matrix.sh              #   parallel `compare` driver (used by `tinderbox-ng continue`)
│   ├── compare-merge-emerge.py        #   plan-correctness analyzer (used by `tinderbox-ng analyze`)
│   ├── consolidate-phase-stats.py     #   forecast aggregator (used by `tinderbox-ng phase-stats`)
│   ├── extract-timing.py              #   wall-clock timing extractor (used by `tinderbox-ng extract-timing`)
│   └── run-matrix.sh                  #   in-baseline test runner (installed as `tinderbox-matrix`)
│
├── share/tinderbox-ng/                # Pure data: templates copied into the baseline + fixtures
│   ├── baseline.make.conf             #   /etc/portage/make.conf for baseline
│   ├── baseline.package.use           #   /etc/portage/package.use/00-tinderbox-ng-defaults
│   ├── baseline.package.accept_keywords  #   unmask sys-apps/portage ** for portage-9999
│   ├── baseline.repos.conf            #   /etc/portage/repos.conf/gentoo.conf
│   ├── portage-ng-dev.in              #   in-chroot launcher (template)
│   ├── manifest-100.txt               #   smoke manifest (100 atoms)
│   ├── manifest-1000.txt              #   release-comparison manifest (1000 atoms)
│   ├── manifest-all.txt               #   legacy tree-scanned cat/pn list (~19k atoms)
│   └── manifest-all-packages.txt      #   kb-derived cat/pn list (regenerate via gen-manifest-from-kb.py)
│
├── share/man/man8/
│   └── tinderbox-ng.8                 # manpage (→ /usr/local/share/man/man8/)
│
├── contrib/                           # Dev-machine helpers (NEVER deployed to the VM)
│   ├── deploy-host.sh                 #   one-shot host install (git clone + symlink + doctor)
│   ├── deploy-baseline.sh             #   safe scp of a single template (with @-token substitution)
│   ├── render-compare-matrix.py       #   render compare-matrix TSVs to Markdown
│   └── easy-pkgs.sh                   #   small smoke driver over a curated package list
│
└── reports/                           # Historical compare-matrix snapshots (Markdown)
```

Each script has exactly one home, decided by *who runs it and why*:
"`tinderbox-ng` runs this internally" → `libexec/`; "I run this on the
VM" → `bin/`; "I run this on my dev machine" → `contrib/`; "this is
data, not code" → `share/`.

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
`flock(1)`, `gpg(1)`, `curl(1)`, `git(1)`, `tar(1)`, the
in-kernel `overlay` module, and (for the test matrix) `app-admin/moreutils`
for `ts(1)` (optional — falls back to plain `tee`).

`tinderbox-ng doctor` aggregates every prerequisite and reports all problems
in one pass; `bootstrap` runs it implicitly so a missing tool surfaces *before*
stage3 download.

### Recommended: `contrib/deploy-host.sh` (one-shot)

```sh
# From your dev machine - install or update a VM from GitHub (default):
contrib/deploy-host.sh root@vm-linux.local

# First-time install on a fresh VM (long: ~hours):
TINDERBOX_BOOTSTRAP_SELFTEST=1 \
  contrib/deploy-host.sh --bootstrap root@vm-linux.local

# Push portage-ng into an existing baseline (does NOT regenerate kb.qlf):
contrib/deploy-host.sh --refresh-portage-ng root@vm-linux.local
```

`deploy-host.sh` performs, in order:

1. `git clone` / `git fetch` of
   [tinderbox-ng](https://github.com/pvdabeel/tinderbox-ng) into
   `/usr/local/share/tinderbox-ng/` on the remote (same model as
   `contrib/ami-tinder.sh` on AWS). There is no rsync. portage-ng is NOT
   installed on the host — it is git-cloned straight into the baseline by
   `bootstrap` / `refresh-portage-ng`; no host `/opt/portage-ng`, that path
   exists only inside the baseline chroot after bootstrap.
2. Symlink `/usr/local/sbin/tinderbox-ng` →
   `/usr/local/share/tinderbox-ng/bin/tinderbox-ng`, plus
   `/usr/local/share/man/man8/tinderbox-ng.8` →
   `/usr/local/share/tinderbox-ng/share/man/man8/tinderbox-ng.8` (so
   `man tinderbox-ng` works without extending `MANPATH`).
3. `tinderbox-ng doctor` on the remote (preflight checks; fails fast on
   missing prerequisites before any heavy work starts).
4. Optional: `tinderbox-ng bootstrap` (with `--bootstrap`).
5. Optional: `tinderbox-ng selftest` (with `--selftest` or
   `TINDERBOX_BOOTSTRAP_SELFTEST=1` during a `--bootstrap` run).

Forwarded environment: `TINDERBOX_NG_URL`, `TINDERBOX_NG_REF`,
`PORTAGE_NG_URL`, `PORTAGE_NG_REF` (portage-ng is git-cloned straight into
the baseline; there is no host-side portage-ng checkout), plus
`TINDERBOX_CCACHE_MAX_SIZE`, `STAGE3_*`, `GENTOO_PROFILE`, etc. — see
`contrib/deploy-host.sh --help`.

### Manual install (equivalent steps)

```sh
# On the VM, clone the rig from GitHub (commit + push your work first):
ssh vm-linux.local sudo git clone https://github.com/pvdabeel/tinderbox-ng.git \
  /usr/local/share/tinderbox-ng

# Symlink the entry point onto $PATH:
ssh vm-linux.local sudo ln -sf \
  /usr/local/share/tinderbox-ng/bin/tinderbox-ng \
  /usr/local/sbin/tinderbox-ng

# Confirm prerequisites:
ssh vm-linux.local sudo tinderbox-ng doctor
```

The script auto-detects two install dirs: `LIBEXEC_DIR` (executable
helpers) and `SHARE_DIR` (data templates), defaulting to
`../libexec/tinderbox-ng/` and `../share/tinderbox-ng/` relative to the
script's `bin/`. Override per bucket with `TINDERBOX_LIBEXEC_DIR` and
`TINDERBOX_SHARE_DIR`. The legacy single-bucket `TINDERBOX_LIB_DIR`
still works (maps to both) for pre-reorg flat installs.

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
9. `git clone`s `portage-ng` from `$PORTAGE_NG_URL` @ `$PORTAGE_NG_REF`
   (default `https://github.com/pvdabeel/portage-ng.git` @ `master`)
   directly into `baseline/opt/portage-ng` (in-chroot path
   `/opt/portage-ng`). There is no host-side portage-ng checkout and no
   rsync; the baseline IS the clone. `refresh-portage-ng` later updates it
   in place with `git fetch` + `reset --hard`.
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
    `=sys-apps/portage-9999` (overrideable via `TINDERBOX_SELFTEST_TARGET` or
    `TINDERBOX_BASELINE_PORTAGE_TARGET`) in a
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
sudo tinderbox-ng refresh-portage-ng                  # git fetch new portage-ng source into baseline
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
`bootstrap_install_portage_ng` step). Use it after new commits land on
`PORTAGE_NG_URL` @ `PORTAGE_NG_REF`: every fresh session bind-mounts the
baseline copy, so without this step new sessions keep running yesterday's
resolver. It unfreezes, runs `git fetch` + `reset --hard origin/$PORTAGE_NG_REF`
inside the baseline clone, refreshes the in-baseline
`/usr/local/bin/portage-ng-dev` shim plus the `tinderbox-matrix` helper,
then re-freezes. It does **not** regenerate
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
checkout (against commit `4ba5099c`):

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

  Wired upstream as of portage-ng commit `76730972` ("binpkg: honor
  `config:binpkg_refresh/1` in `available_for/4`"): every dispatch
  probe now reads `config:binpkg_refresh/1`, and under `mtime` it
  stats `$PKGDIR/Packages` and re-runs `binpkg:sync(kb)` whenever an
  external producer (sibling matrix worker) has bumped the index.
  Long-running `--build` workers therefore pick up newly-minted
  binpkgs between probes instead of being frozen on the snapshot
  loaded by `kb:register` at process start. Concurrent probes
  serialize on a dedicated upstream-side mutex. Verified end-to-end
  on 2026-05-15 with a `compare --build app-containers/dive` smoke:
  binpkg short-circuit fires on every install action, completing in
  ~2 minutes vs the prior multi-hour source-build path.
- As of [`4ba5099c`](https://github.com/pvdabeel/portage-ng/commit/4ba5099c)
  (issue #80), `binpkg_exec:ensure_index_fresh/0` mtime-gates full
  `Packages` re-parses inside each standalone process, gates verbose
  `% Binpkg:` scroll behind `--verbose` (so `--ci` compare logs split
  on `[step N]` instead), and registers freshly built gpkgs via
  `config:binpkg_self_inject(true)` without re-reading the whole index.
  `doctor` warns when the baseline or host checkout predates this commit;
  run `refresh-portage-ng` after updating the host checkout.
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

**Lowest known-good commit:**
[`pvdabeel/portage-ng@4ba5099c`](https://github.com/pvdabeel/portage-ng/tree/4ba5099c)
— anything from this commit forward is verified end-to-end on the
matrix harness. It includes the binpkg dispatch refresh policy
([`76730972`](https://github.com/pvdabeel/portage-ng/commit/76730972)),
the `--ci --build` VDB-reconciliation backstop
([`8deb4131`](https://github.com/pvdabeel/portage-ng/commit/8deb4131)),
and the mtime-gated binpkg index refresh + in-memory self-inject from
[issue #80](https://github.com/pvdabeel/portage-ng/issues/80)
([`4ba5099c`](https://github.com/pvdabeel/portage-ng/commit/4ba5099c)).
Earlier extraction-era pin `26069d06` predates all three fixes and is
no longer recommended. If `PORTAGE_NG_REF` tracks a known-incompatible
commit and bootstrap fails, pin `PORTAGE_NG_REF=4ba5099c` until the
contract is restored.

`compare` and `compare-matrix` always invoke portage-ng as
`--mode standalone` (never `--mode ipc`). Each compare session owns an
isolated overlay VDB; upstream's ipc daemon serializes requests one at a
time and shares the daemon's ROOT, so it would break parallel matrix
workers even though it keeps a warm binpkg index. Standalone processes
still benefit from #80 automatically after `refresh-portage-ng`: the
first `ensure_index_fresh/0` in a `--build` pass syncs the index once,
subsequent probes in the same process are mtime-gated, and freshly built
gpkgs register via `config:binpkg_self_inject(true)` without a full
re-parse.

### CLI surface

`portage-ng-dev --mode standalone` accepts the following flags and any
combination of them in the cases tinderbox-ng exercises:

| Flag | Used by | Semantic |
|------|---------|----------|
| `--ci` | every in-chroot invocation | non-interactive; required for stable exit codes |
| `--sync` | bootstrap, `refresh-kb` | populates `Knowledge/kb.qlf` (and `profile.qlf`) |
| `--pretend` | `cmd_compare`, `cmd_portage_ng`, `tinderbox-matrix` | plan only |
| `--build` | `cmd_compare`, `cmd_portage_ng`, `easy-pkgs.sh` | plan then execute (one SWI-Prolog process) |
| `--timeout N` | (optional, only when wrapper template forwards it) | per-invocation watchdog |

### Exit-code triage

portage-ng's `Source/Application/Interface/exitcodes.pl` is the source of
truth for numeric exit codes. `libexec/tinderbox-ng/portage-ng-exit-label.py`
reads that table at compare time (from the baseline copy under
`/opt/portage-ng/`) and maps codes to the labels written into `results.tsv`:

| Code | Label | Meaning |
|------|-------|---------|
| 0 | `OK` | Clean plan (no assumptions) |
| 1 | `OK(cycles)` | Plan with prover cycle-break assumptions only |
| 2 | `OK(assumed)` | Plan with ≥1 domain assumption (e.g. masked dep) |
| 3 | `FAIL(plan)`, `FAIL(build)`, or `FAIL(target)` | Plan/build step failed, or no resolvable target (log-heuristic disambiguation) |
| 1 (no plan footer) | `FAIL(cli)` | Interface catch-all / CLI failure misreported as rc 1 |
| other | `FAIL(N)` | Unexpected non-zero exit (matches emerge's `FAIL(N)` shape) |
| 124 / 137 / 143 | `TIMEOUT` / `KILLED(...)` | watchdog / signal |

Log-based reclassifiers in `_compare_summarize` may further override labels
(e.g. `RESTRICT(fetch)`, `INFRA(overlay-inode-flicker)`).

`compare-matrix.sh` and `_compare_summarize` treat any label matching
`OK` or `OK(...)` as "plan produced". If you add codes to `exitcodes.pl`
in portage-ng, extend `NAME_LABEL` in `portage-ng-exit-label.py` (or rely
on the generic `name_to_label()` fallback) so matrix output stays readable.

### Output formats

- The planner footer must contain `Total: <N> action[s]`.
- The build summary must contain `Total: <N> completed`.
- ANSI escape sequences around the numbers are tolerated (tinderbox-ng
  strips them with `sed -E 's/\x1b\[[0-9;]*[a-zA-Z]//g'`).
- `--sync` postcondition: `Knowledge/kb.qlf` exists at the portage-ng
  repo root after the run.

### Filesystem layout (portage-ng repo root)

`tinderbox-ng` git-clones `portage-ng` from GitHub into the baseline at
`/opt/portage-ng/`. The wrapper requires the following at the repo root:

- `portage-ng.pl` — project entry point (used as `swipl -f`).
- `Source/loader.pl` — module loader (alternative repo marker).
- `Source/Config/<hostname>.pl` — host-specific config; hostname inside
  the chroot is `vm-linux.local`.
- `Source/Knowledge/Sets/world/<hostname>` — world file (created empty
  by bootstrap if missing).

The following gitignored paths are produced inside the baseline by
`--sync`/runtime and are not in the source repo:

- `Knowledge/{kb.qlf, kb.raw, profile.qlf, profile.raw, embeddings.pl,
  phase_stats.pl, resume.pl}`
- `Source/{Snapshots, Certificates, Private}/`
- `Source/Knowledge/Sets/**/*.local`

`refresh-portage-ng` updates the clone with `git fetch` + `reset --hard`,
which only rewrites tracked files — so these untracked, gitignored
artefacts survive untouched without any explicit exclude list.

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

Logs land in `/srv/tinderbox-ng/logs/compare-<label>-<stamp>/` and are
split per engine into a plan log (resolver output) and a build log
(execution output):

* `portage-ng.plan.log` + `portage-ng.plan.log.exit` (always present)
* `portage-ng.build.log` + `portage-ng.build.log.exit` (only in `--build`
  mode, and only if the plan pass succeeded)
* `emerge.plan.log` + `emerge.plan.log.exit` (always present)
* `emerge.build.log` + `emerge.build.log.exit` (only in `--build` mode,
  and only if the plan pass succeeded)

The plan log is exactly the `--pretend` output (i.e. `emerge -vp
--oneshot`-equivalent for emerge, `portage-ng-dev --mode standalone --ci
--pretend` for portage-ng); the build log is the same engine without
`--pretend`. Absence of `<engine>.build.log` signals the plan pass
failed and the build pass was skipped.

Build pass also salvages target-only artefacts (regardless of success):

* `portage-ng.target.<cat_pn>.build.log` — portage-ng's per-ebuild log
  for the target, copied out of the session's
  `/var/tmp/portage-ng/logs/` (config:build_log_dir).
* `emerge.target.<cat_pn>.build.log[.gz]` — Portage's per-ebuild log
  for the target. We export `PORTAGE_LOGDIR=/var/log/portage` for the
  build pass so this file survives `FEATURES=clean`'s workdir wipe on
  successful merges.
* `phase_stats.pl` — portage-ng's accumulated per-phase byte and
  wall-clock counts (`ebuild_exec:phase_stats_file`), salvaged from
  `/opt/portage-ng/Knowledge/` on the session's upper layer.

Multi-target compares produce one target build log per target per
engine; single-target produces one file per engine. With `--keep`, the
upper layer of each session is preserved at
`/srv/tinderbox-ng/sessions/pkg-cmp-{portage-ng,emerge}-<label>/upper/`.

Exit code: `0` if both succeeded, `1` if exactly one failed, `2` if both
failed. Useful for CI sweeps.

### Long-running matrix runs (`compare-matrix`)

`libexec/tinderbox-ng/compare-matrix.sh` (installed on the VM as
`/usr/local/sbin/compare-matrix`) drives `tinderbox-ng compare` over a
manifest of atoms (one `cat/pn` per line; `#` comments OK) and writes a
`results.tsv` plus per-package compare logdirs as it goes. The shipped
manifests live in `share/tinderbox-ng/`:

| Manifest                    | Atoms  | Use case                                      |
|-----------------------------|-------:|-----------------------------------------------|
| `manifest-100.txt`          |    100 | Curated smoke set, ~hour at `--build`.        |
| `manifest-1000.txt`         |   1000 | Standard release-comparison run, ~a day.      |
| `manifest-all.txt`          |  19243 | Legacy tree-scanned cat/pn list.              |
| `manifest-all-packages.txt` |  19285 | **Preferred** kb-derived cat/pn list (matches `kb.qlf`). |

Regenerate `manifest-all-packages.txt` after `refresh-kb` (or any kb
rebuild) so the manifest tracks exactly what portage-ng loaded:

```sh
# on the VM (after refresh-kb):
python3 contrib/gen-manifest-from-kb.py \\
  --kb /srv/tinderbox-ng/baseline/opt/portage-ng/Knowledge/kb.raw \\
  --out share/tinderbox-ng/manifest-all-packages.txt
```

A full `--build` sweep over `manifest-all-packages.txt` takes days, so
**the preferred way to launch one is inside a detached `screen`
session**. That keeps a live driver TTY you can reattach to after an
SSH drop, unlike `nohup setsid` (or the equivalent `tinderbox-ng
continue --background`) which detaches into a pure daemon with no TTY:

```sh
# Kick off a fresh matrix detached, from your dev machine:
ssh root@vm-linux.local 'screen -dmS tinderbox-ng \
    compare-matrix --build --jobs 16 \
    --manifest /usr/local/share/tinderbox-ng/share/tinderbox-ng/manifest-all-packages.txt'

# Reattach the live driver TTY:
ssh -t root@vm-linux.local screen -r tinderbox-ng

# Detach again without killing the driver: Ctrl-A d (inside screen).

# Stop cleanly (in-flight comparisons finish; partial results stay in TSV):
ssh root@vm-linux.local screen -S tinderbox-ng -X stuff $'\003'
```

`compare-matrix.sh` writes `results.tsv` incrementally and uses an
flock per row, so a SIGINT or SIGTERM is always safe — every prior row
is durable, and `tinderbox-ng continue` picks up the unfinished tail.

`--jobs N` runs N package comparisons concurrently. Each comparison
itself spawns 2 sessions (portage-ng + emerge in parallel mount
namespaces), so the actual session count peaks at 2N. The vm-linux
baseline (32 cores / 50 GiB RAM) handles `--jobs 16` (32 peak
sessions) cleanly with the sessions tmpfs at 100G; tune to keep load
average under (`nproc` − small headroom).

#### Resuming an interrupted matrix run

If the driver dies (host reboot, `kill -TERM`, screen session closed,
…) the `results.tsv` and per-package logs survive untouched. Resume
into the same `screen` so you again have a live driver TTY:

```sh
# Preferred: re-launch in the same screen session:
ssh root@vm-linux.local screen -dmS tinderbox-ng \
    tinderbox-ng continue --jobs 16

# No-screen fallback: nohup setsid + pidfile + driver log, no live TTY:
ssh root@vm-linux.local sudo tinderbox-ng continue --jobs 16 --background
```

`tinderbox-ng continue` auto-detects the most recent
`compare-matrix-<stamp>/` run dir, reads its `meta.txt` to recover the
original `--pretend`/`--build` mode, manifest path, and `--jobs`
setting, computes `manifest \ done-targets`, and dispatches
`compare-matrix --resume-dir <run>` so the resumed work appends to the
original `results.tsv` (no manual TSV merging). Each resume is logged
in `meta.txt` as a `# ----- resumed at … -----` block. Pass `--run
DIR` to pin a specific previous run instead of auto-detecting; pass
`--jobs N` to override the original `--jobs`. The `--background` flag
is the no-screen equivalent of wrapping in `screen -dmS`; refuses to
launch if a `compare-matrix` driver is already in flight (override
with `--force`, but two parallel matrices oversubscribe the host).

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

### Plan-correctness analysis: `tinderbox-ng analyze`

```sh
sudo tinderbox-ng analyze                       # latest matrix run
sudo tinderbox-ng analyze --run /srv/tinderbox-ng/reports/compare-matrix-...
sudo tinderbox-ng analyze --logdir /srv/tinderbox-ng/logs   # any compare-* dir
sudo tinderbox-ng analyze --md5-cache /srv/tinderbox-ng/baseline/var/db/repos/gentoo/metadata/md5-cache
```

After a `compare-matrix` run finishes you have a `results.tsv` (binary
pass/fail) plus 19k+ pairs of `portage-ng.plan.log` / `emerge.plan.log`
files (one pair per package, in `/srv/tinderbox-ng/logs/compare-*-<stamp>/`).
`tinderbox-ng analyze` feeds those pairs through
`libexec/tinderbox-ng/compare-merge-emerge.py` and produces:

- **Set agreement** (Jaccard at CN, CN+V, CN+V+U granularity) — how
  often the two resolvers select the same packages, with and without
  matching version + USE flags.
- **Ordering** — Kendall tau concordance and Spearman ρ over the per-plan
  package order, restricted to the common-CN intersection.
- **Dependency-aware** (with `--md5-cache`) — Kendall tau restricted to
  pairs with an actual build-dependency edge (`DepConc%`), plus a
  self-consistency check on the merge plan (`Viol%`: how many times a
  build dep appears later than its dependent in portage-ng's plan).
- **Wave-based inversion classification** — every ordering disagreement
  with Portage is classified as `within_wave` (provably independent),
  `cross_wave_merge_confirmed`, `cross_wave_emerge_confirmed`, or
  `cross_wave_no_edge`.
- **Domain assumptions / blockers / cycle breaks** — aggregated
  `KNOWN_TREE_CONFLICTS` are tracked separately as
  `tree_conflict_assumptions`. `install_only_cycle_breaks` flags
  scheduler cycle breaks where every action is `:install` (suspect).
- **Download set agreement** — Jaccard between merge plan and any
  `.fetchonly` sibling.
- **Timing** — wall-clock per pair, distribution stats, and which
  resolver was faster.

Output lands in the source dir (defaults to the matrix run dir):

- `analysis.json` — full structured metrics dump (use `jq` to drill in)
- `analysis.txt`  — captured stdout summary (the line-oriented overview)

`--target-regex` restricts to a subset of pairs by label; `--full-lists`
includes per-pair missing/extra/use-mismatch lists in the JSON (much
larger output, but enables CN-level gap analysis).

### Build-time forecasting: `tinderbox-ng phase-stats`

```sh
sudo tinderbox-ng phase-stats                       # latest matrix run, median
sudo tinderbox-ng phase-stats --run /srv/tinderbox-ng/reports/compare-matrix-...
sudo tinderbox-ng phase-stats --src /srv/tinderbox-ng/logs --aggregator p75
sudo tinderbox-ng phase-stats --out /opt/portage-ng/Knowledge/phase_stats.pl
```

Each `compare --build` session has portage-ng emit a per-session
`phase_stats.pl` (one `phase_bytes/3` + `phase_seconds/3` fact per
`(Entry, Phase)`), which `compare` salvages into the per-package compare
logdir. Across a manifest those files give us many independent
observations of every phase. `tinderbox-ng phase-stats` walks the source
tree, aggregates per `(Entry, Phase)` with a configurable function, and
emits a single master `phase_stats.pl` in the same Prolog format
portage-ng already consumes (`ebuild_exec:load_phase_stats/0` /
`ebuild_exec:expected_phase_stats/4`).

Drop the master file in as the next portage-ng instance's
`Knowledge/phase_stats.pl` to seed its forecast tables — the planner's
progress estimates and ETA become accurate from the very first build of
each package, without waiting for it to be (re)built locally first.

Aggregators:

- `median` — default; robust against cold-cache outliers and dirty disks.
- `mean`   — plain average.
- `max`    — worst-case envelope; useful for upper-bound progress bars.
- `p75` / `p90` — slightly/strongly pessimistic forecasts.

`--min-observations N` drops `(Entry, Phase)` buckets with fewer than
`N` samples (useful when consolidating across heterogeneous fleets).
`--exclude-glob PAT` skips matching paths during the input scan.
The output file is excluded from the input scan automatically, so it's
safe to run in-place.

### Wall-clock timing comparison: `tinderbox-ng extract-timing`

```sh
sudo tinderbox-ng extract-timing                       # latest matrix run
sudo tinderbox-ng extract-timing --run /srv/tinderbox-ng/reports/compare-matrix-...
sudo tinderbox-ng extract-timing --src /srv/tinderbox-ng/logs
sudo tinderbox-ng extract-timing --session \
    /srv/tinderbox-ng/logs/compare-app_misc_jq-20260515T143052
```

Where `analyze` measures plan *correctness* and `phase-stats` aggregates
phase-level *byte/second* observations for forecasting,
`extract-timing` measures end-to-end *wall clock*: how long each engine
actually took to plan and to build. Source data is written by
`cmd_compare` itself — every pass produces a `<log>.timing` companion
file with `started=` / `ended=` / `wall_time_ms=` / `rc=` lines, so the
extractor doesn't need to grep build logs or trust engine-internal
timing markers (emerge has none, and portage-ng's `--ci` path does not
emit them either).

The output JSON has one entry per compare session, indexed by recovered
CPV when extractable from emerge's resolver line, else by session label
(`cat/pn`):

```json
{
  "summary": {
    "sessions_total": 1000,
    "portage_ng_build_p50_ms": 28443,
    "emerge_build_p50_ms": 19200,
    "build_pn_over_em_p50": 1.48,
    "pn_build_faster":  73,
    "em_build_faster": 925,
    "build_tied":        2
  },
  "entries": {
    "dev-libs/popt-1.19-r1": {
      "session": "compare-dev-libs_popt-20260515T161353",
      "portage_ng_plan":  {"started": ..., "wall_time_ms": 4123, "rc": 0},
      "portage_ng_build": {"started": ..., "wall_time_ms": 28443, "rc": 0},
      "emerge_plan":      {"started": ..., "wall_time_ms": 1820, "rc": 0},
      "emerge_build":     {"started": ..., "wall_time_ms": 19200, "rc": 0},
      "ratios": {"plan_pn_over_em": 2.27, "build_pn_over_em": 1.48},
      "phase_stats": {"compile": {"seconds": 14.2, "bytes": 1024000}, ...}
    }
  }
}
```

Older sessions (predating the per-pass `.timing` capture) reconstruct
wall time from each log file's ctime/mtime delta and are flagged with
`mtime_estimated: true`, so consumers know to treat those values as
approximate.

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
  `contrib/deploy-baseline.sh <template> <user@host:remote-path>`
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

- **Compare pipeline integration.** Done — vendored as `tinderbox-ng
  analyze` (plan correctness, `libexec/tinderbox-ng/compare-merge-emerge.py`)
  and `tinderbox-ng extract-timing` (wall-clock,
  `libexec/tinderbox-ng/extract-timing.py`). Both consume per-session
  compare logs directly and no longer require the legacy `.merge` /
  `.emerge` graph layout.
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
| `PORTAGE_NG_URL` | `https://github.com/pvdabeel/portage-ng.git` | portage-ng git URL `git clone`d straight into `baseline/opt/portage-ng` (no host checkout, no rsync). |
| `PORTAGE_NG_REF` | `master` | Ref to deploy. Pin to `4ba5099c` (or later) for issue #80 binpkg perf + prior VDB/binpkg-refresh fixes. |
| `GENTOO_PROFILE` | `default/linux/amd64/23.0/split-usr/no-multilib` | Must match `config:gentoo_profile/1`. |
| `GENTOO_LOCALE` | `en_US.UTF-8 UTF-8` | Appended to `/etc/locale.gen`. |
| `GENTOO_LOCALE_NAME` | `en_US.utf8` | Argument to `eselect locale set`. |
| `TINDERBOX_SESSIONS_TMPFS_SIZE` | `100G` | tmpfs cap for `$TINDERBOX_ROOT/sessions`. Empty/`0` disables. |
| `TINDERBOX_COMPARE_PN_SINGLE_PASS` | `1` | `compare --build` runs portage-ng once (`--build`) and splits logs. Set `0` for legacy separate `--pretend` + `--build` passes. |
| `TINDERBOX_CCACHE_MAX_SIZE` | `100G` | `max_size` written into `/etc/ccache.conf` by `bootstrap_install_ccache` and `install-ccache`. |
| `TINDERBOX_REBOOTSTRAP` | (unset) | If set, `bootstrap` overwrites an existing baseline. |
| `TINDERBOX_SKIP_DOCTOR` | (unset) | If set, `bootstrap` skips its preflight doctor pass. |
| `TINDERBOX_BOOTSTRAP_SELFTEST` | (unset) | If set, `bootstrap` runs `selftest` on completion. |
| `TINDERBOX_BASELINE_PORTAGE_TARGET` | `=sys-apps/portage-9999` | Live Portage atom emerged into baseline; empty keeps stage3 Portage. |
| `TINDERBOX_SELFTEST_TARGET` | `$TINDERBOX_BASELINE_PORTAGE_TARGET` | Atom used by `selftest`'s `compare --pretend`. |
| `TINDERBOX_MIN_FREE_MIB` | `30000` | Disk-space floor (MiB) `doctor` checks under `dirname $TINDERBOX_ROOT`. |
| `TINDERBOX_MDNS_HOSTS` | `mac-pro.local imac-pro.local` | mDNS hostnames `doctor` resolves and `_inject_mdns_hosts` writes into each session's `/etc/hosts`. |
