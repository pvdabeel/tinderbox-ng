# portage-ng improvement clusters — "both engines failed" analysis

**Source run:** `compare-matrix-20260621T174159` (tinderbox-ng, host `vm-linux.local`)
**Span:** 2026-06-21 → 2026-06-27 · **Targets scored:** 19,282 · **Tree pin:** `a4b9340966c1094e85901d229e83df431274e439`
**Baseline profile:** `default/linux/amd64/23.0/split-usr/no-multilib` (headless, `-X`/`-wayland` by default)
**Audience:** portage-ng resolver/build development agent
**Goal:** isolate the cases where **portage-ng AND emerge both failed**, separate genuine
upstream breakage from portage-ng deficiencies, and cluster the deficiencies into
actionable resolver/build improvements.

---

## 1. TL;DR for the portage-ng agent

Out of **1,201** targets where neither engine succeeded:

- **~41 %** are *genuine upstream breakage* — both engines build the package and hit the
  identical compile/configure error (modern GCC/Clang strictness, dead ffmpeg APIs, etc.).
  **portage-ng is correct to fail these. No action.**
- **~10 %** are *fetch-restricted / dead distfiles* (`RESTRICT=fetch`, HTTP 404).
  Not resolver failures; portage-ng already tags them `RESTRICT(fetch)`. Should be
  **excluded from the failure denominator**, not "fixed".
- **~37 %** are **portage-ng-fixable** and fall into four clusters below
  (**A, B, C, D**). They share a striking property: emerge usually could not even
  *produce a plan* (it failed at resolution in **794/1201 = 66 %** of cases), whereas
  **portage-ng planned and entered a real build phase in 548 of those** — portage-ng is
  already strictly ahead on resolution, so these are "so close" cases worth closing.

**Two clusters (A + B, ~280 cases) share one root cause:** portage-ng does not honor
**USE-flag dependency constraints** (`dep[flag]`) — it builds/reuses a dependency with
profile-default USE and never back-propagates the consumer's `[flag]` requirement.
Confirmed by binpkg inspection (`cairo` built `-X` while `gtk+` needs `cairo[X]`).

**Highest-leverage fact:** failures collapse onto a handful of **cascade roots**. Fixing
~6 packages (`kwindowsystem`, `libplasma`, `gtk+`/`cairo[X]`, `gnustep-make`/`gcc[objc]`,
`aeson`/GHC-ABI, `plasma-wayland-protocols`) clears **~300** of the 1,201 failures.

---

## 2. How to read this report (methodology + caveats)

**Dataset.** `results.tsv` rows where `pn_exit ∉ {OK, ?}` AND `em_exit ∉ {OK, ?}` → 1,201 rows.

**Real-failure extraction.** In a multi-package plan the target's own merge log usually
looks *successful*; the real failure is a **dependency**. For each case we scanned the
salvaged per-package logs `build-logs/portage-ng/**/*.build.log.gz` (plus the target log)
for the canonical Portage banner:

```
ERROR: <cat/pkg-ver>::<repo> failed (<phase> phase):
```

The package carrying that banner is the **cascade root**; the `count` next to each root
below is how many *distinct compare targets* it knocked out.

**Engine comparison.** For emerge we distinguished *plan-stage* failure (no build attempted —
`REQUIRED_USE` / "one of the following packages is required" / masked / circular / blocker)
from *build-stage* failure (it built and hit a compile/configure error).

**Caveat — profile bias (read before filing bugs).** The tinderbox baseline is a
**headless `no-multilib` profile** with `USE="-X -wayland …"` defaults. That deliberately
inflates clusters A/B/C (desktop USE flags are off). On a desktop profile many of these
would not surface. **However**, the underlying portage-ng behavior they expose — not
satisfying `dep[useflag]` and toolchain-USE prerequisites — is a real, profile-independent
correctness gap. Verify each proposed fix on the resolver semantics, not just on this
profile.

**Reproduction data (on `vm-linux.local`):**
- Classifier + cluster summary: `python3 contrib/classify-failfail.py <RUN_DIR>`.
  It writes `<RUN_DIR>/failfail-classified.json` (one object per case, with
  `pn_first_fail_pkg`, `pn_first_fail_phase`, `pn_first_fail_sig`, `em_reason`,
  `expectation`, `cluster`) and prints the cluster breakdown + per-cluster cascade
  roots used throughout this report.
- Per-package error region: `python3 contrib/show-fail.py <RUN_DIR>/failfail-classified.json <cat/pkg> [N]`.
- Re-derive from scratch: `tinderbox-ng analyze --run /srv/tinderbox-ng/reports/compare-matrix-20260621T174159`.
- Classifier sources committed in this repo under `contrib/`: `classify-failfail.py`
  (the single classifier), `show-fail.py` (drill-down), `_ff_unmask_probe.py`
  (keyword-filtered/unmask signature probe).

---

## 3. Aggregate results

| Cluster | Cases | % | portage-ng-fixable? |
|---|---:|---:|---|
| **E** — genuine upstream breakage (both engines fail identically) | 490 | 40.8 % | No (correct fail) |
| **A** — Qt6/KDE build-dep gap (`qtbase[wayland]` / `Qt6WaylandClient` / `qtpaths`) | 157 | 13.1 % | **Yes** |
| **B** — `dep[useflag]` gap, non-KDE (mostly `cairo[X]`) | 118 | 9.8 % | **Yes** |
| **F** — fetch-restricted / dead distfile | 118 | 9.8 % | No (not a resolver failure) |
| **H** — merge-time collision / circular-dep / opaque | 122 | 10.2 % | Partly |
| **D** — Haskell/GHC ABI-hash rebuild propagation | 85 | 7.1 % | **Yes** |
| **C** — toolchain/setup USE prerequisite (`gcc[objc]`) | 74 | 6.2 % | **Yes** (baseline + resolver) |
| **Z/G** — misc (resource/OOM, install/unpack/patch) | 37 | 3.1 % | Mixed |

**Engine-stage split (all 1,201):** emerge failed at *plan* stage in **794**; emerge
actually *built* in **395** and matched portage-ng's failure signature in **390 (98.7 %)**.

---

## 4. Cluster deep-dives

### Cluster A — Qt6/KDE build-time dependency gap · 157 cases · **P1**

**Cascade roots:** `kde-frameworks/kwindowsystem` (58), `kde-plasma/libplasma` (38),
`dev-libs/plasma-wayland-protocols` (25), `kde-frameworks/breeze-icons` (13),
`kde-plasma/kwayland` (2), `dev-qt/qtbase` (2), tail of single KDE apps.
**Sample targets:** `app-text/kbibtex`, `app-backup/kup`, `app-office/merkuro`,
`app-accessibility/kontrast`, `app-crypt/keysmith`, `app-portage/kuroo`.

**Symptom (two variants), both at CMake configure:**

```
CMake Error at CMakeLists.txt:68 (find_package):
  Could not find a package configuration file provided by "Qt6WaylandClient"
  (requested version 6.9.0) with any of the following names:
    Qt6WaylandClientConfig.cmake  qt6waylandclient-config.cmake
```
```
CMake Error at /usr/share/ECM/modules/ECMQueryQt.cmake:84 (message):
  No Qt6 qtpaths executable found.  Can't check QT_INSTALL_PREFIX as required
```

**Evidence — the dependency is USE-conditional and was under-provisioned.**
`kde-frameworks/kwindowsystem-6.27.0.ebuild`:

```
IUSE="wayland X"
# wayland backend pulls the Qt6 wayland integration:
RDEPEND=" wayland? ( >=dev-qt/qtbase-${QTMIN}:6=[wayland] ) "
BDEPEND=" wayland? ( >=dev-qt/qtbase-${QTMIN}:6[wayland] dev-util/wayland-scanner )
          wayland? ( dev-libs/plasma-wayland-protocols >=dev-libs/wayland-protocols-1.46 ) "
# and the switch that makes find_package(Qt6WaylandClient) REQUIRED:
mycmakeargs=( -DKWINDOWSYSTEM_WAYLAND=$(usex wayland) )
```

So portage-ng resolved `kwindowsystem[wayland]` (turning the `find_package` REQUIRED),
but the wayland-side provider (`dev-qt/qtbase[wayland]` and/or `dev-qt/qtwayland`, which
ships `Qt6WaylandClientConfig.cmake`) was **not present with the wayland feature**.
Run-wide search confirms `dev-qt/qtwayland` **never built, never failed, and is not in
binpkgs** — it is simply absent from portage-ng's plan, while the rest of the KF6 stack
(KIO, WindowSystem, Svg, …) resolved fine.

**Root-cause hypothesis (portage-ng).** When a package enables its own `wayland` (or `X`)
USE, portage-ng must add the *USE-conditional* build/runtime edges and satisfy the
`dep[wayland]` constraint on `qtbase`. Either (a) the `wayland?(...)`-guarded edges are not
expanded, or (b) `qtbase` is built/reused with profile-default `-wayland`, so
`Qt6WaylandClient` is never produced. This is the same mechanism as Cluster B.

**Why it should not have failed.** portage-ng built 100+ packages of the plan correctly;
the only missing piece is a *declared, discoverable, USE-conditional* dependency edge.

**Proposed investigation / fix.**
1. For a failing target (e.g. `app-text/kbibtex`), dump portage-ng's resolved plan and
   check whether `dev-qt/qtwayland` / `qtbase[wayland]` appear and with which USE.
2. Verify expansion of `wayland? ( … )` / `X? ( … )` conditional dependency groups when
   the *guarding* USE flag is enabled on the consumer.
3. Verify `dep:6[wayland]` slot-USE constraints force `qtbase` to be built/selected with
   `wayland`, not the profile default.

**Acceptance criteria.** `portage-ng -p app-text/kbibtex` lists `dev-qt/qtwayland`
(or `qtbase[wayland]` carrying `Qt6WaylandClientConfig.cmake`) before any
`kde-frameworks/*[wayland]` consumer; a fresh `tinderbox-ng compare kde-frameworks/kwindowsystem`
reaches `compile`.

---

### Cluster B — `dep[useflag]` dependency satisfaction (non-KDE) · 118 cases · **P1 (same root as A)**

**Cascade roots:** `x11-libs/gtk+` (28), `media-libs/allegro` (7),
`media-plugins/kodi-game-libretro` (4), `gui-libs/gtk` (3), `sci-libs/vtk` (3),
`net-libs/gupnp-av` (3); singletons: `seahorse` (`gcr-3`), `gjs` (`spidermonkey`),
`libopenrazer` (`lrelease`/qttools), `mpibash` (MPI).
**Sample targets:** `dev-cpp/gtkmm`, `dev-cpp/gtksourceviewmm`, `dev-util/regexxer`,
`app-misc/gnote`, `app-crypt/seahorse`.

**Symptom (meson/cmake configure):**

```
../gtk-3.24.52/meson.build:491:9: ERROR: Dependency "cairo-xlib" not found
                                         (tried pkg-config and cmake)
```

**Evidence — smoking gun.** `x11-libs/gtk+-3.24.52.ebuild` declares the USE-dep:

```
>=x11-libs/cairo-1.14[aqua?,glib,svg(+),X?,${MULTILIB_USEDEP}]
```

The actual **`cairo` binpkg that sessions consumed** has USE:

```
x11-libs/cairo-1.18.4-r1
USE: abi_x86_64 amd64 elibc_glibc glib kernel_linux      ← no X, no svg, no aqua
```

`cairo` was built with profile-default `-X`, so `cairo-xlib.pc` is absent, so every GTK
consumer needing X fails configure. portage-ng did not propagate `gtk+`'s `cairo[X]`
constraint back onto the `cairo` build (and/or reused a `cairo[-X]` binpkg even though the
consumer required `cairo[X]` — note `make.conf` sets `--binpkg-respect-use=y` for *emerge*,
but portage-ng has its own binpkg matcher).

**Root-cause hypothesis.** Same as A: `dep[flag]` constraints are not enforced when
selecting/building/reusing the dependency. The X case is the common denominator; `gcr-3`,
`spidermonkey`, qttools `lrelease`, and MPI are the same pattern with different flags/tools.

**Proposed investigation / fix.**
1. Implement/repair `dep[useflag]` and `dep[flag?]` (conditional) constraint propagation so
   a consumer's `[X]`/`[svg]`/etc. forces the dependency to be (re)built with that flag.
2. Audit binpkg reuse: a binpkg whose stored USE does not satisfy the consumer's
   `dep[flag]` constraint must be rejected (the emerge `--binpkg-respect-use` equivalent).

**Acceptance criteria.** `portage-ng -p x11-libs/gtk+` shows `cairo` resolved with `X`
enabled; a fresh `tinderbox-ng compare x11-libs/gtk+` reaches `compile`.

---

### Cluster C — toolchain / setup USE prerequisite (`gcc[objc]`) · 74 cases · **P2**

**Cascade root:** `gnustep-base/gnustep-make` (51) + assorted out-of-tree kernel-module /
ObjC packages.
**Sample targets:** `app-arch/unar`, `app-laptop/tuxedo-control-center-bin`,
`media-sound/a2jmidid`, `dev-db/pgpool2`.

**Symptom (pkg_setup phase):**

```
* gnustep-make-2.9.3 requires a working Objective-C runtime and a compiler with
* Objective-C support. Your current settings lack these requirements
* Please switch your active compiler to gcc with USE=objc, or clang
* ERROR: gnustep-base/gnustep-make-2.9.3-r2::gentoo failed (setup phase):
*   Could not find Objective-C runtime
```

**Evidence.** Baseline `sys-devel/gcc` VDB `USE` = `cxx fortran` — **no `objc`**.
The compiler in the baseline cannot build Objective-C, so every ObjC consumer dies in setup.

**Two-level fix.**
- *Baseline (tinderbox-ng side):* add `objc` (and likely `objc-gc`) to the baseline `gcc`
  USE in `share/tinderbox-ng/baseline.make.conf` / `baseline.package.use`, then
  `tinderbox-ng baseline-thaw` + rebuild gcc. **This is a tinderbox change, tracked here for
  completeness.**
- *portage-ng side:* the resolver should recognize the implicit
  `sys-devel/gcc[objc]` (or `||( gcc[objc] clang )`) prerequisite declared by these
  packages and either schedule a `gcc` rebuild or surface a clear unmet-prerequisite error
  at plan time, instead of dispatching a build that dies in `pkg_setup`.

**Acceptance criteria.** With `gcc[objc]` in the baseline, `tinderbox-ng compare
app-arch/unar` reaches `compile`. (Independently: portage-ng flags the missing
`gcc[objc]` at plan time when the baseline lacks it.)

---

### Cluster D — Haskell / GHC ABI-hash rebuild propagation · 85 cases · **P2**

**Cascade roots:** `dev-haskell/aeson` (48), plus `dev-ml/ocaml-compiler-libs` (6),
`dev-haskell/invariant` (6), `hashtables` (5), `tasty` (4), `indexed-traversable-instances` (3).
**Sample targets:** `app-text/pandoc-cli`, `app-misc/geneweb`, `dev-haskell/aeson`,
`dev-haskell/citeproc`.

**Symptom (haskell-cabal configure):**

```
ghc-pkg check: 'checking for other broken packages:'
installed package semigroupoids-5.3.7 is broken due to missing package
  bifunctors-5.6.3-9AmA3NO9963FDwV9BBcxcZ
...
* Detected broken packages: semigroupoids-5.3.7 semialign-1.3
* ERROR: dev-haskell/aeson-2.1.2.1::gentoo failed (configure phase):
*   //==-- Please, run 'haskell-updater' to fix broken packages --==//
```

**Root-cause hypothesis.** A Haskell library (`bifunctors`) was rebuilt with a new GHC ABI
hash; its reverse-dependencies (`semigroupoids`, `semialign`, …) still reference the *old*
hash in `ghc-pkg`, so the next Haskell build's `ghc-pkg check` declares them broken. This is
exactly what Gentoo's `haskell-updater` / `@preserved-rebuild` handles. portage-ng does not
model GHC ABI hashes and therefore does not trigger the reverse-dependency rebuild.

**Proposed investigation / fix.**
1. Model GHC package ABI hashes (the `-<hash>` suffix in `ghc-pkg`) as part of the
   dependency identity for `dev-haskell/*` (and the analogous OCaml `dev-ml/*` registry).
2. When a Haskell library's ABI hash changes, schedule rebuilds of its installed
   reverse-deps before building new consumers — i.e. native `haskell-updater` semantics.

**Acceptance criteria.** After building/upgrading `dev-haskell/bifunctors`, a plan for
`dev-haskell/aeson` includes rebuilds of `semigroupoids`/`semialign`; `ghc-pkg check` is
clean before `aeson` configures.

---

### Cluster F — fetch-restricted / dead distfile · 118 cases · **not a resolver failure**

**Sample targets:** `app-arch/stuffit`, `app-office/moneydance`, `dev-db/sqldeveloper`
(`RESTRICT=fetch`, need manual download); `app-crypt/bcwipe` (upstream URL → HTTP 404).

portage-ng already classifies these as `RESTRICT(fetch)` / download error. **Recommendation
(reporting, not resolver):** exclude `RESTRICT(fetch)` and confirmed-dead-distfile cases
from the "failure" denominator so they don't dilute genuine signal. No portage-ng code
change required.

---

### Cluster H — merge-time collisions / circular deps / opaque · 122 cases · **P3, mixed**

`pn_fail_pkg` is empty here: the build failed **without** emitting a standard
`failed (<phase> phase)` banner. Sub-structure (by emerge's view):

- **File collisions with a baseline provider (~9+, `em=blocker`):** `app-arch/hardlink`
  (collides with `sys-apps/util-linux` over `/usr/bin/hardlink`), `dev-libs/libiconv`,
  `dev-libs/libintl` (glibc), `dev-libs/libelf` (elfutils). The package is effectively
  obsolete/absorbed. portage-ng fails at merge:

  ```
  * Detected file collision(s):
  *   /usr/bin/hardlink
  * sys-apps/util-linux-2.41.4-r1:0::gentoo
  * Package 'app-arch/hardlink-0.3.2' NOT merged due to file collisions.
  ```

  *Improvement:* detect the installed provider / implicit blocker at **plan** time and
  refuse/deconflict early rather than failing in the merge phase.
- **Perl circular deps (~8, `em=circular`):** `dev-perl/Bio-*`. Worth checking
  portage-ng's circular-dependency break heuristics.
- **Remainder (~100):** opaque (failure without a banner — possibly merge-time, EAPI
  helper, or a portage-ng-internal error). **Needs a manual sampling pass** — flagged as
  follow-up, not yet root-caused.

---

### Cluster E — genuine upstream breakage · 490 cases · **no action (correctly failed)**

Both engines build the package and hit the **same** error. Documented here only so the
portage-ng agent does *not* chase them. Cascade roots: `app-text/sdcv` (29 — knocks out the
stardict dictionaries via `const gchar*`→`gchar*` `-fpermissive`), `mail-mta/netqmail` (10 —
K&R prototypes are errors under GCC 14), `media-video/mplayer` (8 — `libavutil/avutil.h`
missing under ffmpeg6), `dev-lang/python`, `app-text/texlive-core`, `games-engines/love`.

Representative (identical on both engines):

```
app-accessibility/yasr:  error: too many arguments to function 'funcs[kf->index].f'; expected 0, have 1
app-text/sdcv:           error: invalid conversion from 'const gchar*' to 'gchar*' [-fpermissive]
```

**Note — cascade amplification:** one broken leaf inflates the headline count
(`sdcv` → 29 stardict packages; `aeson` → 48 Haskell packages). Distinct *root* failures are
far fewer than 1,201; deduplicating by cascade root is the right way to size the work.

---

## 5. Prioritized roadmap for portage-ng

| Priority | Cluster | Est. cases cleared | Effort | Note |
|---|---|---:|---|---|
| **P1** | **A + B** — honor `dep[useflag]` / `flag?(...)` conditional deps & binpkg-USE matching | ~275 | Medium–High | Single core resolver-correctness feature; A and B are the same root |
| **P2** | **D** — GHC/OCaml ABI-hash reverse-dep rebuild (`haskell-updater` semantics) | ~85 | Medium | Well-defined precedent |
| **P2** | **C** — recognize toolchain-USE prerequisites (`gcc[objc]`); + tinderbox baseline fix | ~74 | Low (baseline) / Medium (resolver) | Baseline `gcc[objc]` is a quick partial win |
| **P3** | **H** — plan-time collision/blocker detection; circular-dep heuristics; sample opaque tail | ~30+ | Low–Medium | Plus a manual triage pass |
| n/a | **F** | — | — | Exclude from failure metric; no code change |
| n/a | **E** | — | — | Genuine upstream; do not pursue |

**Single most valuable change:** implement/repair **USE-flag dependency constraint
satisfaction** (Clusters A+B). It is a correctness gap independent of the headless profile
and unblocks the largest, most coherent group of "so close" failures where portage-ng
already out-planned emerge.

---

## 6. Appendix — verification recipes

```sh
RUN=/srv/tinderbox-ng/reports/compare-matrix-20260621T174159

# Re-run the classifier: cluster breakdown + per-cluster cascade roots on stdout,
# full per-case data written to $RUN/failfail-classified.json
python3 contrib/classify-failfail.py "$RUN"

# Inspect a cluster root's real error
python3 contrib/show-fail.py "$RUN/failfail-classified.json" kde-frameworks/kwindowsystem 1
python3 contrib/show-fail.py "$RUN/failfail-classified.json" x11-libs/gtk+ 1
python3 contrib/show-fail.py "$RUN/failfail-classified.json" dev-haskell/aeson 1

# Confirm the dep[use] root cause
grep -A12 '^CPV: x11-libs/cairo-1.18' /srv/tinderbox-ng/shared/binpkgs/Packages   # USE has no X
cat /srv/tinderbox-ng/baseline/var/db/pkg/sys-devel/gcc-*/USE                      # has no objc

# Single-package repro (fresh sessions, both engines)
tinderbox-ng compare kde-frameworks/kwindowsystem
tinderbox-ng compare x11-libs/gtk+
tinderbox-ng compare dev-haskell/aeson
```

**Data locations on `vm-linux.local`:** matrix run
`/srv/tinderbox-ng/reports/compare-matrix-20260621T174159/`; per-package compare logdirs
`/srv/tinderbox-ng/logs/compare-<label>-<stamp>/` (each with `portage-ng.{plan,build}.log`,
`portage-ng.target.<cp>.build.log`, and salvaged `build-logs/portage-ng/**.build.log.gz`).
