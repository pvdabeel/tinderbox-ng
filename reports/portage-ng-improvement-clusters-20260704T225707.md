# portage-ng improvement clusters — "both engines failed" analysis

**Source run:** `compare-matrix-20260704T225707` (tinderbox-ng, host `vm-linux.local`)
**Span:** 2026-07-04 → 2026-07-12 (resumed 3× after host reboots) · **Targets scored:** 19,287
**Tree pin:** `a4b9340966c1094e85901d229e83df431274e439`
**Baseline profile:** `default/linux/amd64/23.0/split-usr/no-multilib` (headless, `-X`/`-wayland` by default)
**Audience:** portage-ng resolver/build development agent
**Prior report:** `reports/portage-ng-improvement-clusters-20260621T174159.md`
**Goal:** isolate the cases where **portage-ng AND emerge both failed**, separate genuine
upstream breakage from portage-ng deficiencies, and cluster the deficiencies into
actionable resolver/build improvements.

---

## 1. TL;DR for the portage-ng agent

### Run headline (all targets, not just fail/fail)

| Engine | OK | Rate |
|---|---:|---:|
| portage-ng | 17,834 | **92.5 %** |
| emerge | 14,365 | 74.5 % |
| Both OK | 14,360 | 74.5 % |
| **pn OK / em FAIL** | **3,474** | portage-ng strictly ahead |
| Both FAIL | 1,333 | 6.9 % |

portage-ng produced a buildable plan on **3,474** targets emerge could not even resolve.
Plan concordance on the 19,285 scored pairs: **87.55 %** (Spearman ρ = 0.868), **0** cycle
breaks, **1,645** domain assumptions.

### fail/fail subset (1,447 cases)

Out of **1,447** targets where neither engine succeeded (classifier count; includes
KILLED/TIMEOUT variants beyond the 1,333 strict `FAIL/FAIL` TSV rows):

- **~68 %** are *genuine upstream breakage* (cluster **E**, 980 cases). The headline
  number is dominated by a **single cascade root**: `sec-policy/selinux-base` (301
  targets) — an upstream Gentoo eclass bug (`selinux-policy-2.eclass` missing
  `BDEPEND=sys-apps/semodule-utils`), not a portage-ng resolver gap. portage-ng correctly
  applied `libselinux[python]` etc.; the missing command is simply undeclared in metadata.
  See [portage-ng#102](https://github.com/pvdabeel/portage-ng/issues/102) for the proposed
  builder→prover feedback / re-plan design.
- **~10 %** are *fetch-restricted / dead distfiles* (cluster **F**, 151). Not resolver
  failures; exclude from the failure denominator.
- **~16 %** are **portage-ng-fixable** across clusters **A–D** (233 cases total). Down
  from ~434 in the June run — the KDE/Qt6 wave (cluster A) largely cleared (157 → 18).
- emerge failed at *plan* stage in **925 / 1,447 = 64 %**; emerge actually *built* in
  **521** and matched portage-ng's failure signature in most of those.

**Highest-leverage fixable facts (unchanged themes, updated counts):**

| Cluster | Cases | Top cascade root | Priority |
|---|---:|---|---|
| **B** — `dep[useflag]` gap | 91 | `media-libs/allegro` (6) | **P1** |
| **D** — GHC/OCaml ABI rebuild | 88 | `dev-haskell/aeson` (49) | **P2** |
| **C** — toolchain/setup USE prereq | 36 | `sys-fs/zfs` (3) | **P2** |
| **A** — Qt6/KDE build-dep gap | 18 | `dev-qt/qtbase` (6) | **P1** (tail) |

Fixing `dep[useflag]` satisfaction (cluster B, same root as A) plus GHC ABI propagation
(cluster D) clears the bulk of the remaining actionable fail/fail set. The selinux cascade
is upstream metadata + optional runtime discovery (not a classic resolver bug).

---

## 2. How to read this report (methodology + caveats)

**Dataset.** Wrapper `*.log` files under the matrix run dir where both `pn_exit` and
`em_exit` are non-OK → **1,447** cases classified.

**Real-failure extraction.** In a multi-package plan the target's own merge log usually
looks *successful*; the real failure is a **dependency**. For each case we scanned the
salvaged per-package logs `build-logs/portage-ng/**/*.build.log[.gz]` (plus the target
log) for the canonical Portage banner:

```
ERROR: <cat/pkg-ver>::<repo> failed (<phase> phase):
```

The package carrying that banner is the **cascade root**; the `count` next to each root
below is how many *distinct compare targets* it knocked out.

**Engine comparison.** For emerge we distinguished *plan-stage* failure (no build attempted —
`REQUIRED_USE` / masked / circular / blocker / `use_dep_unsat`) from *build-stage* failure
(it built and hit a compile/configure error).

**Caveat — profile bias.** The tinderbox baseline is a **headless `no-multilib` profile**
with `USE="-X -wayland …"` defaults. That inflates clusters A/B/C on a desktop profile
many would not surface. The underlying portage-ng behavior they expose — not satisfying
`dep[useflag]` and toolchain-USE prerequisites — is a real, profile-independent
correctness gap.

**Caveat — infra failures (exclude from resolver scorecard).** Five targets show **pn FAIL /
em OK** (`sys-devel/gcc` = ENOSPC on shared tmpfs under `--jobs 16`; four others need
manual triage). Fifteen `FAIL/KILLED(SIGTERM)` and six `TIMEOUT/FAIL` pairs are parallel-run
artefacts, not resolver gaps.

**Reproduction data (on `vm-linux.local`):**
- Classifier: `python3 contrib/classify-failfail.py <RUN_DIR>` →
  `<RUN_DIR>/failfail-classified.json` + stdout cluster breakdown.
- Per-package error region: `python3 contrib/show-fail.py <RUN>/failfail-classified.json <cat/pkg>`.
- Plan metrics: `tinderbox-ng analyze --run /srv/tinderbox-ng/reports/compare-matrix-20260704T225707`.
- Classifier sources: `contrib/classify-failfail.py`, `contrib/show-fail.py`,
  `contrib/_ff_unmask_probe.py`.

---

## 3. Aggregate results

### 3a. fail/fail cluster table

| Cluster | Cases | % | portage-ng-fixable? | Δ vs June run |
|---|---:|---:|---|---|
| **E** — genuine upstream breakage | 980 | 67.8 % | No (correct fail) | +490 (↑ selinux cascade) |
| **F** — fetch-restricted / dead distfile | 151 | 10.4 % | No | +33 |
| **B** — `dep[useflag]` gap, non-KDE | 91 | 6.3 % | **Yes** | −27 |
| **D** — Haskell/GHC ABI-hash rebuild | 88 | 6.1 % | **Yes** | +3 |
| **H** — merge-time collision / opaque | 83 | 5.7 % | Partly | −39 |
| **C** — toolchain/setup USE prerequisite | 36 | 2.5 % | **Yes** | −38 |
| **A** — Qt6/KDE build-dep gap | 18 | 1.2 % | **Yes** | **−139** |

**Fixable A–D total: 233** (was 434 in June).

### 3b. User-expectation grouping (fail/fail only)

| Bucket | Count | Meaning |
|---|---:|---|
| genuine_break | 521 | emerge built and broke — real upstream |
| resolver_gap | 778 | emerge refused at plan; pn tried anyway |
| expected_build | 77 | emerge blocked on keyword/license only |
| ok_to_fail | 70 | emerge blocked on package.mask |
| unknown | 1 | |

**Actionable resolver_gap** (emerge plan-rejected, pn built, cluster ∉ {E,F}): **197**
cases — pn should either complete the build or refuse cleanly at plan time.

### 3c. Engine-stage split (all 1,447)

| emerge stage | Count |
|---|---:|
| PLAN (never built) | 925 |
| BUILD (built and broke) | 521 |
| unknown | 1 |

---

## 4. Cluster deep-dives

### Cluster E — genuine upstream breakage · 980 cases · **no action (correctly failed)**

Both engines build (or portage-ng builds while emerge also would) and hit a **real**
compile/configure error. **Do not chase these as resolver bugs.**

**Cascade roots (top 10):**

| Root | Targets blocked |
|---|---:|
| `sec-policy/selinux-base` | **301** |
| `app-text/sdcv` | 29 |
| `mail-mta/netqmail` | 11 |
| `media-video/mplayer` | 8 |
| `dev-python/flit-core` | 6 |
| `sci-libs/rocBLAS` | 6 |
| `dev-python/html5lib` | 5 |
| `dev-util/xdelta` | 5 |
| `games-engines/love` | 5 |
| `sci-mathematics/unuran` | 5 |

#### selinux-base cascade (301) — upstream eclass bug, not USE-dep gap

**Symptom (compile phase):**

```
make: /usr/bin/semodule_package: No such file or directory
ERROR: sec-policy/selinux-base-2.20260312_p1::gentoo failed (compile phase)
```

**Root cause.** `semodule_package` is provided by `sys-apps/semodule-utils`, but
`selinux-policy-2.eclass` does **not** declare it as `BDEPEND`. portage-ng correctly
resolved and built everything in the declared closure (including `libselinux[python]` for
`policycoreutils`). The failure is **undeclared metadata**, not a `dep[useflag]` miss.

**portage-ng improvement (optional, not classic resolver):**
[portage-ng#102](https://github.com/pvdabeel/portage-ng/issues/102) — builder emits a
structured "missing provider" diagnostic → persisted `feedback:discovered_dep/4` → prover
re-derives plan with provider ordered first. Plans stay **derived, never patched** (no
builder-side `repair_rebuild` injection).

**Other E roots (unchanged from June).** `sdcv` (const-gchar* strictness), `netqmail`
(K&R prototypes under GCC 14), `mplayer` (ffmpeg API drift) — identical signatures on both
engines when emerge reaches build.

---

### Cluster A — Qt6/KDE build-time dependency gap · 18 cases · **P1 (tail)**

**Down from 157 in June** — most KDE stack targets now pass or fail only on emerge's plan
stage. Remaining cases are a **tail** of KDE apps still hitting `dev-qt/qtbase` or direct
compile failures.

**Cascade roots:** `dev-qt/qtbase` (6), singleton KDE apps (`juk`, `kapptemplate`, `dolphin`,
`keditbookmarks`, `kget`, `krfb`, `basket`, `krusader`, `kde-cli-tools`).

**Symptom (unchanged mechanism).** CMake cannot find `Qt6WaylandClient` or `qtpaths` when a
consumer enables `wayland` USE but `qtbase[wayland]` / `dev-qt/qtwayland` was not
provisioned — same `dep[useflag]` / conditional-BDEPEND gap as cluster B.

**Sample targets:** `kde-apps/juk`, `kde-apps/kapptemplate`, `kde-apps/kget`.

**Proposed fix.** Same as June report §4 Cluster A: honor `wayland? ( … )` conditional edges
and `dep:6[wayland]` slot-USE constraints; force `qtbase` rebuild when consumer requires
`[wayland]`.

**Acceptance criteria.** `portage-ng -p kde-apps/juk` lists `dev-qt/qtwayland` (or
`qtbase[wayland]`) before KDE consumers; `tinderbox-ng compare kde-apps/juk` reaches
`compile`.

---

### Cluster B — `dep[useflag]` dependency satisfaction (non-KDE) · 91 cases · **P1**

**Cascade roots:**

| Root | Targets |
|---|---:|
| `media-libs/allegro` | 6 |
| `media-plugins/kodi-game-libretro` | 6 |
| `sci-libs/vtk` | 3 |
| `net-libs/gupnp-av` | 3 |
| `x11-terms/rxvt-unicode` | 3 |
| `app-accessibility/kontrast` | 2 |
| `app-cdr/dvdisaster` | 2 |
| `dev-libs/libopenrazer` | 2 |

**Failure signatures:** configure_cmake (40), compile (40), configure_meson (11).

**Sample targets:** `app-accessibility/kontrast`, `app-crypt/seahorse`, `app-office/denaro`,
`dev-libs/gjs` (root: `dev-lang/spidermonkey`), `app-shells/mpibash` (root:
`sys-cluster/libcircle`).

**Symptom (representative):**

```
CMake Error: Could NOT find KF6 (missing: KirigamiAddons)
ERROR: Dependency "cairo-xlib" not found
```

**Root-cause hypothesis (unchanged).** `dep[flag]` constraints are not enforced when
selecting/building/reusing dependencies. Profile-default `-X`/`-wayland` binpkgs are reused
even when a consumer requires `cairo[X]` or similar.

**Proposed fix.**
1. Implement/repair `dep[useflag]` and `flag?(...)` conditional constraint propagation.
2. Reject binpkgs whose stored USE does not satisfy the consumer's `dep[flag]` constraint
   (emerge `--binpkg-respect-use` equivalent).

**Acceptance criteria.** `portage-ng -p x11-libs/gtk+` shows `cairo` with `X` enabled;
`tinderbox-ng compare media-libs/allegro` reaches `compile`.

---

### Cluster C — toolchain / setup USE prerequisite · 36 cases · **P2**

**Cascade roots:** `sys-fs/zfs` (3), `app-laptop/tuxedo-drivers` (2),
`games-fps/serioussam-tse-data` (2), `sys-apps/apparmor` (2); singletons:
`vendor-reset`, `ola`, `pglogical`, `pgpool2`, `avr-libc`, `mingw64-runtime`.

**Symptom (setup/compile phase):** kernel-module builds missing `PYTHON_SINGLE_TARGET`,
cross-toolchain packages missing `avr-gcc`, ObjC runtime checks (`gcc[objc]` class).

**Two-level fix (unchanged).**
- *Baseline:* ensure `sys-devel/gcc[objc]` (and kernel-headers alignment) in baseline USE.
- *portage-ng:* recognize implicit toolchain-USE prerequisites at **plan** time instead of
  dispatching builds that die in `pkg_setup`.

**Acceptance criteria.** `tinderbox-ng compare sys-fs/zfs` reaches `compile` with correct
`PYTHON_SINGLE_TARGET` and kernel module toolchain satisfied.

---

### Cluster D — Haskell / GHC ABI-hash rebuild propagation · 88 cases · **P2**

**Cascade roots:**

| Root | Targets |
|---|---:|
| `dev-haskell/aeson` | **49** |
| `dev-ml/ocaml-compiler-libs` | 6 |
| `dev-haskell/hashtables` | 6 |
| `dev-haskell/free` | 4 |
| `dev-haskell/invariant` | 4 |
| `dev-haskell/tasty` | 4 |

**Symptom (haskell-cabal configure):**

```
ghc-pkg check: installed package semigroupoids-5.3.7 is broken due to missing package
  bifunctors-5.6.3-...
* Detected broken packages: semigroupoids-5.3.7 semialign-1.3
* Please, run 'haskell-updater' to fix broken packages
```

**Root-cause hypothesis (unchanged).** portage-ng does not model GHC ABI hashes and does not
trigger reverse-dependency rebuilds when a library's hash changes — native `haskell-updater` /
`@preserved-rebuild` semantics.

**Note.** `fixup:ghcabi` (#93) exists but uses builder-local `repair_rebuild`; long-term fix
should propagate through the prover like any other dep closure change.

**Acceptance criteria.** After upgrading `dev-haskell/bifunctors`, a plan for
`dev-haskell/aeson` includes rebuilds of broken reverse-deps; `ghc-pkg check` is clean
before `aeson` configures.

---

### Cluster F — fetch-restricted / dead distfile · 151 cases · **not a resolver failure**

**Top roots:** `games-fps/ut2004-bonuspack-ece` (4), `sci-electronics/labone` (2),
`games-fps/ut2004-bonuspack-mega` (2); singletons across commercial/game fetch-restrict
packages.

portage-ng already classifies many as `RESTRICT(fetch)` / download error. **Recommendation:**
exclude from the failure denominator. No resolver code change required.

---

### Cluster H — merge-time collisions / circular deps / opaque · 83 cases · **P3, mixed**

**Sample targets:** `app-containers/snapd`, `games-util/game-device-udev-rules`,
`games-action/descent2-data`, `dev-qt/qttools`, `dev-lang/lazarus`.

Sub-structure:
- **REQUIRED_USE unsatisfied at setup** (~snapd, game-device-udev-rules): emerge and pn both
  fail early; portage-ng should surface at plan.
- **Opaque / no banner** (~80 in resolver_gap ∩ H): failure without standard phase banner;
  needs manual sampling.
- **Install-phase self-failures in expected_build bucket** (3 cases with keyword-only emerge
  block): possible portage-ng merge bugs — see §5.

---

## 5. "portage-ng should have succeeded" — actionable subset

| Category | Count | Notes |
|---|---:|---|
| Fixable clusters A–D | **233** | Core resolver/build gaps |
| resolver_gap excluding E/F | **197** | pn proved plan emerge couldn't; should build or refuse at plan |
| Overlap (both) | 117 | Same cases counted twice |
| **Unique actionable (approx.)** | **~313** | 233 + (197−117) |
| Strict `should_have` flag | 1 | `www-apps/gitea` — trivial `acct-user/git` install fail |
| expected_build (keyword/license) | 77 | 40 compile = genuine once keyworded; **3 install** = pn merge bugs |
| pn FAIL / em OK | 5 | Infra / manual triage (`sys-devel/gcc` = ENOSPC) |

**Install-phase failures in expected_build** (emerge keyword-only block, pn should merge):
`app-shells/shish`, `dev-lisp/cmucl`, plus game/macOS niche packages — worth a focused
merge-phase audit.

---

## 6. Prioritized roadmap for portage-ng

| Priority | Item | Est. cases | Effort | Note |
|---|---|---:|---|---|
| **P0** | Upstream: Gentoo `selinux-policy-2.eclass` add `BDEPEND=sys-apps/semodule-utils` | 301 | Upstream | Not portage-ng resolver; optional runtime discovery via [#102](https://github.com/pvdabeel/portage-ng/issues/102) |
| **P1** | **B (+ A tail)** — honor `dep[useflag]` / conditional deps & binpkg-USE matching | ~109 | Medium–High | Single core feature; A nearly cleared |
| **P2** | **D** — GHC/OCaml ABI-hash reverse-dep rebuild | ~88 | Medium | `haskell-updater` semantics; avoid builder-only repair |
| **P2** | **C** — toolchain-USE prerequisites (`gcc[objc]`, `PYTHON_SINGLE_TARGET`) | ~36 | Low–Medium | Baseline + plan-time detection |
| **P3** | **H** — plan-time collision/REQUIRED_USE; sample opaque tail | ~83 | Low–Medium | Manual triage pass still needed |
| n/a | **F** | — | — | Exclude from failure metric |
| n/a | **E** (excl. selinux upstream) | ~679 | — | Genuine upstream; do not pursue |

**Single most valuable resolver change (unchanged):** implement/repair **USE-flag dependency
constraint satisfaction** (cluster B). portage-ng already beats emerge on 3,474 targets;
closing B converts "proved but failed at configure" into green builds.

**Infra recommendation (tinderbox-ng side):** bump `tinderbox-sessions` tmpfs or lower
`--jobs` for heavy builds — `sys-devel/gcc` ENOSPC under 16 parallel compares is not a
portage-ng defect.

---

## 7. Delta vs June 2026 run

| Metric | June (`20260621T174159`) | July (`20260704T225707`) |
|---|---:|---:|
| Targets | 19,282 | 19,287 |
| fail/fail | 1,201 | 1,447 |
| pn OK rate | — | **92.5 %** |
| pn OK / em FAIL | — | **3,474** |
| Fixable A–D | ~434 | **233** |
| Cluster A (KDE) | 157 | **18** |
| Cluster E | 490 | **980** (↑ selinux-base 301) |
| Order concordance | — | **87.55 %** |

**Interpretation.** portage-ng improved dramatically on overall success and KDE resolution.
The fail/fail count rose mainly because the selinux policy stack entered the manifest and
cascaded from one upstream metadata hole — inflating cluster E, not indicating resolver
regression. Actionable portage-ng work (A–D) **shrunk by ~46 %**.

---

## 8. Appendix — verification recipes

```sh
RUN=/srv/tinderbox-ng/reports/compare-matrix-20260704T225707

# Classifier: cluster breakdown + failfail-classified.json
python3 contrib/classify-failfail.py "$RUN"

# Inspect cascade roots
python3 contrib/show-fail.py "$RUN/failfail-classified.json" sec-policy/selinux-base 1
python3 contrib/show-fail.py "$RUN/failfail-classified.json" media-libs/allegro 1
python3 contrib/show-fail.py "$RUN/failfail-classified.json" dev-haskell/aeson 1
python3 contrib/show-fail.py "$RUN/failfail-classified.json" kde-apps/juk 1

# Plan correctness metrics
tinderbox-ng analyze --run "$RUN"

# Single-package repro
tinderbox-ng compare sec-policy/selinux-base
tinderbox-ng compare media-libs/allegro
tinderbox-ng compare dev-haskell/aeson
```

**Data locations on `vm-linux.local`:**
- Matrix run: `/srv/tinderbox-ng/reports/compare-matrix-20260704T225707/`
- Classified JSON: `.../failfail-classified.json`
- Plan analysis: `.../analysis.{json,txt}`
- Per-package logs: `/srv/tinderbox-ng/logs/compare-<label>-<stamp>/`

**Related design doc:** builder→prover feedback for undeclared build deps —
`reports/portage-ng-builder-feedback-replan-spec.md` /
[portage-ng#102](https://github.com/pvdabeel/portage-ng/issues/102).
