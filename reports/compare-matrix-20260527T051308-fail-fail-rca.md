# fail/fail RCA — `compare-matrix-20260527T051308` (latest)

**Matrix:** `/srv/tinderbox-ng/reports/compare-matrix-20260527T051308/results.tsv`  
**Rows:** **72** packages with portage-ng FAIL and emerge FAIL (`--build`).  
**Regenerate:** `contrib/analyze-fail-fail.py` on `vm-linux.local`.

---

## Executive summary

| Category | Count | portage-ng bug? | Action |
|----------|------:|-----------------|--------|
| stardict → `app-text/sdcv` (glib 2.88) | 28 | **No** | Gentoo/upstream patch |
| Same-atom compile (both engines) | 29 | **No** | ebuild/toolchain |
| Kernel OOT modules (`Module.symvers`) | 6 | **No** | `install-kernel-sources` |
| verify + unsatisfied assume | 7 | **Yes** | [#9](https://github.com/pvdabeel/portage-ng/issues/9), [#10](https://github.com/pvdabeel/portage-ng/issues/10) |
| Plan domain / masked / acct ordering | 3 | **Yes** | [#14](https://github.com/pvdabeel/portage-ng/issues/14), [#15](https://github.com/pvdabeel/portage-ng/issues/15) |
| Fetch / collision | 2 | **No** | upstream / profile |

**~59/72** are not portage-ng planner bugs. **13** map to open issues **#9–#15** (no new issues required for this matrix pass).

---

## A. stardict (28) — not portage-ng

All `app-dicts/stardict-*` fail on BDEPEND **`app-text/sdcv-0.5.5`** (both pn and em):

```
gunicode.h:837: invalid conversion from 'const gchar*' to 'gchar*'
  (g_utf8_next_char in stardict_lib.cpp)
```

Target dictionary never reached. One upstream/ebuild fix covers the whole cluster.

---

## B. portage-ng — verify / assume (#9, #10)

| Target | Assumed provider | pn dies on |
|--------|------------------|------------|
| `app-accessibility/caribou` | `media-libs/clutter` (+ glib dbus update) | `caribou` — clutter.pc missing |
| `app-accessibility/kontrast` | `dev-qt/qtbase` | `kde-plasma/kwayland` |
| `app-cdr/xfburn` | `xfce-base/libxfce4ui` | `xfce-base/exo` configure |
| `app-backup/kup` | `dev-qt/qtbase` | `kde-plasma/libplasma` |
| `app-admin/systemdgenie` | `dev-qt/qtbase` | KDE stack (plan rc=2) |
| `app-crypt/keepsecret` | `dev-qt/qtbase` | `kde-plasma/libplasma` |
| `app-crypt/keysmith` | `dev-qt/qtbase` | `kde-plasma/libplasma` |

Issues: [#9](https://github.com/pvdabeel/portage-ng/issues/9) (caribou/glib), [#10](https://github.com/pvdabeel/portage-ng/issues/10) (assume + unsatisfied_constraints).

---

## C. portage-ng — plan policy (#14, #15)

| Target | Issue | Symptom |
|--------|-------|---------|
| `acct-user/buildbot` | [#14](https://github.com/pvdabeel/portage-ng/issues/14) | `acct-group/buildbot **` assumed; `useradd: group does not exist` |
| `app-containers/snapd` | [#15](https://github.com/pvdabeel/portage-ng/issues/15) | `verify systemd (masked)` but `USE=systemd` → REQUIRED_USE fail |
| `app-admin/mkosi` | (systemd stack) | Fails on `systemd-260.1-r1`; large plan |

---

## D. Kernel modules (6) — not portage-ng

| Package | Blocker |
|---------|---------|
| `app-antivirus/lkrg` | `Module.symvers` not found |
| `app-emulation/vendor-reset` | `linux-mod-r1` setup — kernel tree not built |
| `app-cdr/cdemu`, `cdemu-daemon`, `gcdemu`, `kcdemu` | `sys-fs/vhba` same setup error |

Fix: baseline `tinderbox-ng install-kernel-sources` / `manifest-kernel-modules.txt`.

---

## E. Fetch / collision (2) — not portage-ng

| Package | Cause |
|---------|-------|
| `app-crypt/bcwipe` | `Couldn't download BCWipe-1.9-13.tar.gz` |
| `app-arch/hardlink` | File collision with `sys-apps/util-linux` (`/usr/bin/hardlink`) |

---

## F. Same-atom compile (29) — not portage-ng

Both engines fail on the **same** package (representative):

| Package | Failure type |
|---------|----------------|
| `app-accessibility/julius`, `yasr` | legacy C configure/compile |
| `app-admin/clsync`, `multilog-watch` | C API drift |
| `app-admin/ryzen_smu` (+ `ryzen_monitor`) | setup phase |
| `app-crypt/gpgstats` | C++ const conversion |
| `app-editors/*` (aee, ee, dhex, shed, ted, …) | termios / C compile / link |
| `app-emacs/edb`, `emms`, `auctex` | elisp / TeX build-time |
| `app-emulation/bochs` | `libltdl` missing |
| `app-emulation/glean` | `pkg_resources` (Python) |
| `app-cdr/dvdisaster`, `app-editors/scite`, … | configure/compile (see logs) |

No new portage-ng issues — planner and emerge agree on the failing atom.

---

## Open portage-ng issues (this matrix)

| # | Title | fail/fail rows |
|---|--------|----------------|
| [9](https://github.com/pvdabeel/portage-ng/issues/9) | caribou glib dbus + clutter | caribou |
| [10](https://github.com/pvdabeel/portage-ng/issues/10) | assume + unsatisfied_constraints | caribou, kontrast, xfburn, kup, systemdgenie, keepsecret, keysmith |
| [14](https://github.com/pvdabeel/portage-ng/issues/14) | buildbot acct ordering | buildbot |
| [15](https://github.com/pvdabeel/portage-ng/issues/15) | snapd masked systemd | snapd |

Related (pn-fail / em-ok, not fail/fail): [#11](https://github.com/pvdabeel/portage-ng/issues/11) VDB reconciliation, [#12](https://github.com/pvdabeel/portage-ng/issues/12) dosemu binpkg, [#13](https://github.com/pvdabeel/portage-ng/issues/13) sequoia-sqv LLVM lock.

---

## Commands

```sh
# On VM
MATRIX=/srv/tinderbox-ng/reports/compare-matrix-20260527T051308/results.tsv \
  python3 contrib/analyze-fail-fail.py

awk -F'\t' '$3 ~ /FAIL/ && $4 ~ /FAIL/ {print $1}' \
  /srv/tinderbox-ng/reports/compare-matrix-20260527T051308/results.tsv | wc -l
```
