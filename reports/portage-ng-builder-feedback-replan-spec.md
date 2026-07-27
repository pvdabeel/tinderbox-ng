# portage-ng design spec: builder→prover feedback and plan re-derivation

**Status:** proposal / hand-off to portage-ng dev agent
**Repo affected:** `portage-ng` (NOT tinderbox-ng — this repo only produces the
inputs and observes the failures)
**Companion:** `reports/portage-ng-improvement-clusters-20260621T174159.md`
(the cluster report that motivated this)
**Motivating live case:** `sec-policy/selinux-base` compile dies with
`semodule_package: command not found` (exit 127), cascading to 300+
`sec-policy/selinux-*` packages. Root cause is an **undeclared** build dep:
`selinux-policy-2.eclass` never lists `sys-apps/semodule-utils` in `BDEPEND`.

---

## 1. Goal

When a build fails at runtime for a reason that maps to a **missing provider**
(a command/file that some package on disk would supply), portage-ng should
**not** have the builder patch reality in place. Instead:

> the builder emits a *structured diagnostic*, that diagnostic becomes
> *learned knowledge*, the pipeline *re-derives* a fresh provable plan that
> orders the provider before the target, and the builder *resumes* that new
> plan.

Plans are **derived, never patched.** This is the explicit design constraint:
no builder-side auto-injection into an in-flight plan.

## 2. Why not the existing fixup path

`Source/Domain/Gentoo/Exceptions/fixup.pl` already gives a clean per-phase
retry registry (`fixup:phase_retry_hook/10`, dispatched from
`ebuild_exec:run_phases_unlocked` at the non-zero-exit branch). But every
current mechanism — `ghcabi` (#93), `ocamlabi` (#99), `collision` (#90) —
resolves the fault with `fixup:repair_rebuild/4`: the **builder rebuilds a
package in place, mid-flight, out of dependency order.**

That is exactly the pattern to avoid here because it:

- decides ordering imperatively in the builder instead of proving it;
- cannot chase transitive needs of the newly-required provider;
- mutates the executed artifact, so the plan no longer equals
  `prove_plan(Goals, KB)` (breaks `plancompare.pl` / `analyze` fidelity);
- forgets the discovery — the next run fails again.

## 3. Architecture recap (as-is)

- **Pipeline** (`Source/Pipeline/pipeline.pl`):
  `pipeline:prove_plan_with_fallback/5..7` runs prover → planner → scheduler
  and returns `Plan`, with a 5-tier committed-choice relaxation ladder
  (`pipeline:fallback_tiers/1`). The plan is a pure function of
  `(Goals, KnowledgeBase)`.
- **Builder** (`Source/Pipeline/builder.pl`): `builder:build/1` → pipeline →
  `builder:run_plan/6` → `execute_plan → execute_step → run_action_with_phases`.
  Already has `builder:build_resume/0` and `builder:apply_vdb_reconciliation/4`
  (plan is already reconciled against on-disk VDB reality).
- **Phase runner** (`Source/Domain/Gentoo/Ebuild/ebuild_exec.pl` ~L1016/1052):
  on non-zero phase exit calls `fixup:maybe_phase_retry/9`.
- **Printer** (`Source/Pipeline/printer.pl`): already renders **domain
  assumptions** (`rule(assumed(X), [])`) separately from prover cycle-breaks.

## 4. Proposed design — three seams, each on the correct module boundary

### Seam 1 — Diagnose (Exceptions/ side): signal, don't repair

New mechanism `Source/Domain/Gentoo/Exceptions/missing_provider.pl`, registered
like the others (`fixup:mechanism(missing_provider).`,
`fixup:mechanism_note/3`). Its `phase_retry_hook/10` **must not** call
`repair_rebuild`; it records a discovery and threads the **original** exit code
through unchanged (the phase legitimately still failed):

```prolog
:- multifile fixup:mechanism/1.
fixup:mechanism(missing_provider).

fixup:phase_retry_hook(missing_provider, _Ebuild, Entry, Phase, LogPath,
                       _Use, _Callback, _SizeBefore, EC0, EC0) :-
    EC0 =\= 0,
    missing_provider:scan_missing_cmd(LogPath, Cmd),      % '(\S+): command not found', 127
    missing_provider:provider_of(Cmd, Provider),          % concrete mapping or fail
    feedback:record_discovery(Entry, Provider, bdepend,
                              cmd_not_found(Cmd, Phase)).
    % EC0 passed through unchanged: no re-run here.
```

`missing_provider:provider_of/2` must be **conservative** (see §6): an
authoritative file→package index where available, plus a small curated seed
table for the known-hard commands, e.g.:

```prolog
missing_provider:provides_command(semodule_package, 'sys-apps/semodule-utils').
missing_provider:provides_command(semodule,         'sys-apps/policycoreutils').
```

### Seam 2 — Feedback channel: a learned dependency edge in the KB

A dynamic, persisted predicate — the *only* new state the resolver consumes:

```prolog
:- dynamic feedback:discovered_dep/4.   % Target, Dep, Kind(bdepend|rdepend), Evidence
```

- `feedback:record_discovery/4` dedups and `assertz`es, then appends to
  `Knowledge/feedback.pl`.
- `Knowledge/feedback.pl` is baseline-internal / gitignored (same class as
  `kb.qlf`, `phase_stats.pl`, `resume.pl`) and consulted at startup, so a
  one-time runtime discovery becomes durable knowledge.
- **`rules.pl` is the only resolver file that changes**: when computing BDEPEND
  for `Target`, union in `feedback:discovered_dep(Target, Dep, bdepend, _)` and
  emit it as `rule(assumed(...), [])` so the printer surfaces it under **"Domain
  assumptions"** — fully explainable, exactly like today's cycle-breaks.

### Seam 3 — Control loop: replan & resume (builder ↔ pipeline boundary)

`builder:build/1` becomes a bounded loop instead of a single prove→run:

```prolog
builder:build(Goals) :-
    builder:build_loop(Goals, 0).

builder:build_loop(Goals, Attempt) :-
    pipeline:prove_plan_with_fallback(Goals, _Proof, _Model, Plan, _Trig),
    builder:run_plan(Plan, 1, [], _Completed, Failed, _Stubs),
    ( builder:new_discoveries_since(Attempt, Failed),   % feedback grew during this pass
      Attempt < MaxReplan
    -> A1 is Attempt + 1,
       builder:build_loop(Goals, A1)                    % re-prove with augmented KB
    ;  true ).
```

On the re-proof, the provider is now part of the closure, so the
planner/scheduler order it **before** the target. Everything already built
satisfies from VDB/binpkg (existing `apply_vdb_reconciliation` fast path), so
the retry pass only builds the provider and recompiles the target.

## 5. Walkthrough on the motivating case

1. `sec-policy/selinux-base` compile → `semodule_package: command not found`,
   exit 127.
2. `missing_provider` hook: `Cmd = semodule_package` →
   `Provider = sys-apps/semodule-utils` (seed table). Records
   `feedback:discovered_dep('sec-policy/selinux-base-<v>',
   'sys-apps/semodule-utils', bdepend, cmd_not_found(semodule_package, compile))`
   and persists it. Phase still returns failure.
3. Wave finishes with `selinux-base` failed; `build_loop` sees a new discovery,
   re-enters the pipeline.
4. `rules.pl` now yields `BDEPEND(selinux-base) ⊇ {sys-apps/semodule-utils}`.
   Prover proves it; scheduler orders `semodule-utils` first; printer lists it
   as a domain assumption.
5. Retry pass: `semodule-utils` builds, `selinux-base` recompiles green. The
   300+ downstream `selinux-*` targets never fail — the discovery is persisted
   before their turn.

## 6. Guardrails (must-haves)

- **Bounded replan** (`MaxReplan`, small) + discovery dedup → no loops.
- **Concrete mapping only.** If `provider_of/2` cannot map the command to a
  real package, do nothing new — fall back to today's behavior (fail the target
  cleanly, like emerge). No silent guessing.
- **Classify the gap.** Distinguish *declared-but-unbuilt* dep (a genuine
  resolver/scheduler bug — log loudly, do NOT paper over) from *undeclared* dep
  (the upstream-ebuild case this feature targets). Only the latter should mint a
  `discovered_dep`.
- **Provenance.** Every `discovered_dep` carries `Evidence`; the record doubles
  as an upstream ebuild bug report
  (`selinux-policy-2.eclass` missing `BDEPEND=sys-apps/semodule-utils`).

## 7. Why this is the clean version

- **Single source of truth:** plan stays `= prove_plan(Goals, KB)`.
  Reproducible, cacheable; `plancompare.pl` / tinderbox `analyze` still compare a
  real proof output.
- **Ordering & transitivity for free:** the prover handles the full closure,
  cycles, and the relaxation tiers — a builder-local rebuild cannot.
- **Learning:** the discovery is durable KB knowledge → future runs plan it
  proactively and it can be exported to fix the ebuild upstream.
- **Separation of concerns** matches existing boundaries: `Exceptions/`
  diagnoses, `rules.pl` augments the dependency relation, `pipeline` re-derives,
  `printer` explains.

## 8. Implementation checklist (portage-ng repo)

- [ ] `Source/Domain/Gentoo/Exceptions/missing_provider.pl`: mechanism +
      `scan_missing_cmd/2` (log-tail regex, exit 127) + `provider_of/2`
      (file-index lookup + curated seed) + `mechanism_note/3`.
- [ ] `feedback` module: `discovered_dep/4`, `record_discovery/4`,
      persistence to `Knowledge/feedback.pl`, startup consult.
- [ ] `rules.pl`: union `feedback:discovered_dep(_, _, bdepend, _)` into BDEPEND
      as `rule(assumed(...), [])`.
- [ ] `builder.pl`: `build_loop/2` + `new_discoveries_since/2`; bounded by
      `MaxReplan`.
- [ ] `printer.pl`: confirm discovered deps render under "Domain assumptions"
      (should be automatic if emitted as `rule(assumed(_), [])`).
- [ ] `.gitignore`: add `Knowledge/feedback.pl`.
- [ ] Tests: `Source/Test/` — a selinux-base-style fixture asserting the
      discovery is recorded, the re-proof orders the provider first, and the
      retry succeeds; plus a negative test (unmappable command → clean failure,
      no discovery).
