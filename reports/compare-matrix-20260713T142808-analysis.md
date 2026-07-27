# compare-matrix-20260713T142808 — post-run analysis

**Run:** `/srv/tinderbox-ng/reports/compare-matrix-20260713T142808`  
**Manifest:** `/srv/tinderbox-ng/manifests/manifest-all-packages-20260713T115228Z.txt` (19,384 atoms)  
**Finished:** 2026-07-20T20:45:07Z (host rebooted vm-linux.local after prior session)  
**Mode:** `--build --jobs 16`

## Headline results

| Outcome | Count | % |
|---|---:|---:|
| **Both OK** | 13,996 | 72.2% |
| **portage-ng OK / emerge FAIL** | 3,659 | 18.9% |
| **Both FAIL** | 1,135 | 5.9% |
| **portage-ng FAIL / emerge OK** (regressions) | 594 | 3.1% |

Plan-correctness (`tinderbox-ng analyze --run …`):

- Pairs analysed: 19,382  
- Order concordance (Kendall): **86.06%**  
- Spearman ρ: **0.8376**  
- Domain assumptions: 4,231  
- Missing from portage-ng plans: 670 atoms  
- Extra in portage-ng plans: 22,641 atoms  

Artifacts on VM:

- `results.tsv`, `analysis.{json,txt}`, `failfail-classified.json`
- Per-package wrappers: `$RUN/*.log`
- Compare logdirs: `/srv/tinderbox-ng/logs/compare-<label>-<stamp>/`

---

## (a) portage-ng FAIL / emerge OK — 594 regressions

**Root cause is essentially one bug:** `dev-haskell/text-1.2.5.0-r1` is the first failed atom in **592 / 594** regression plans (verified by scanning every wrapper log's `portage-ng.build.log`).

| First-fail atom | Targets |
|---|---:|
| `dev-haskell/text-1.2.5.0-r1` | 592 |
| `app-crypt/rainbowcrack-1.8` | 1 (fetch: connection refused) |
| `dev-util/llvm-mingw64-13.0.0-r5` | 1 |

### Mechanism (Haskell / GHC)

Typical repro: `dev-haskell/alex` (emerge OK, portage-ng FAIL).

portage-ng merges `dev-lang/ghc-9.8.4-r1`, then reaches `dev-haskell/text-1.2.5.0-r1` and dies at configure/install with:

```
Error: setup: Encountered missing or private dependencies:
bytestring >=0.10.4 && <0.12,
deepseq >=1.1 && <1.5,
ghc-prim >=0.2 && <0.9,
template-haskell >=2.5 && <2.19
```

emerge succeeds on the same target because it schedules the GHC boot-library / ABI registration chain correctly (or runs the equivalent of `haskell-updater` ordering).

This is the **mirror image** of cluster D fail/fail (ABI reverse-deps): here portage-ng **over-builds** into stale/missing `ghc-pkg` state and fails where emerge's plan is sound.

**GitHub:** [portage-ng#108](https://github.com/pvdabeel/portage-ng/issues/108) (new — follow-up to closed #93 / #104).

---

## (b) Both FAIL — where portage-ng could succeed (314 actionable, excl. E/F)

Classifier: `python3 contrib/classify-failfail.py $RUN` → 1,133 fail/fail total.

| Cluster | Count | Theme | portage-ng could fix? |
|---|---:|---|---|
| **B** | 196 | USE-dep / configure gap | **Yes** — prover should refuse like emerge |
| **H** | 43 | collision / opaque merge | Partial — plan-time collision detect (#90) |
| **D** | 34 | GHC/OCaml ABI rebuild | **Yes** — prover-level (#104 incomplete) |
| **C** | 28 | toolchain USE (gcc[objc], …) | **Yes** — plan-time (#105) |
| **A** | 13 | KDE/Qt X11 header closure | **Yes** — USE propagation / baseline |
| E | 666 | genuine upstream break | No |
| F | 153 | fetch-restrict / dead distfile | No (not resolver) |

### Cluster B — 196 cases (emerge `use_dep_unsat`, portage-ng still builds)

Top cascade roots:

| Root pkg | Blocked targets |
|---|---:|
| `dev-libs/uriparser` | 103 |
| `sys-devel/gcc` | 31 |
| `media-libs/allegro` | 6 |

Example `dev-libs/uriparser`: emerge rejects at PLAN (`use_dep_unsat` via `uriparser[doc]` → graphviz chain); portage-ng proves and builds anyway, then configure fails. Same class as closed **#103** but **196 cases remain** in this run.

**GitHub:** [portage-ng#109](https://github.com/pvdabeel/portage-ng/issues/109).

### Cluster A — 13 KDE/Qt cases (X11 headers)

On minimal `23.0/no-multilib` profile (`USE=X` off; only `kwindowsystem[wayland]` forced in baseline `package.use` per #97):

| Target | Failure |
|---|---|
| kget | `KX11Extras: No such file or directory` |
| khelpcenter | `KStartupInfo: No such file or directory` |
| krfb | `X11_Xdamage_LIB NOTFOUND` (missing `libXdamage` dep) |
| kaddressbook, kdepim-addons | `KPim6PimCommonActivities` / `ki18n_wrap_ui` (`pimcommon[activities]`) |

Same class as closed **#106** (18 → 13 after wayland fix; X/activities tail persists). Not fixable by builder feedback #102 (learned deps are bare package edges, no USE).

**GitHub:** [portage-ng#110](https://github.com/pvdabeel/portage-ng/issues/110).

### Cluster D — 34 fail/fail (Haskell ABI)

Root `dev-haskell/text` blocks 20 targets (configure: `Encountered missing or private dependencies`). Overlaps regression root atom but in fail/fail both engines break — different consumer targets.

**GitHub:** folded into #108 follow-up on #104.

### Cluster C — 28 toolchain USE

Roots: `sys-fs/zfs` (3), `sys-devel/gcc` (31 in B overlaps), games-fps data packages needing `gcc[objc]`. Closed **#105** — still present.

### should_have_succeeded — 1 case

`www-apps/gitea`: emerge `use_dep_unsat`; portage-ng built plan including `acct-user/git` then failed trivially.

**GitHub:** [portage-ng#111](https://github.com/pvdabeel/portage-ng/issues/111).

---

## Recommended next steps

1. **#108** — Repro `tinderbox-ng compare --build dev-haskell/alex`; fix GHC boot-lib ordering before `dev-haskell/text`.
2. **#109** — Repro `tinderbox-ng compare --build dev-libs/uriparser`; align PLAN rejection with emerge on `use_dep_unsat`.
3. **#110** — Either baseline `kwindowsystem X` + `pimcommon activities`, or prover USE-closure from consumer headers.
4. tinderbox-ng baseline: consider `profiles/.../desktop` or targeted `package.use` for KDE matrix sanity (see README matrix docs).

Verification recipes:

```sh
RUN=/srv/tinderbox-ng/reports/compare-matrix-20260713T142808
python3 contrib/classify-failfail.py "$RUN"
tinderbox-ng analyze --run "$RUN"
tinderbox-ng compare --build dev-haskell/alex
tinderbox-ng compare --build kde-apps/kget
```
