# Release checks

Reviewed on 2026-11-21. The check uses fake hardware and recorded system-service
transactions; no package updates, firmware flashes or hardware writes were performed.

- Unit suite: 1099 discovered tests run (2 intentional skips), plus 45 standalone tests in
  `tests/test_updates.py`.
- Full UI matrix: `tests/ui/qa_lint.py` covers all 21 pages, dialogs and transient states across
  58 combinations (five sizes, three themes, and 17 stress modes; 4 combinations in `--quick`).
  The UI geometry, page metadata, widget polish, page, and QA logic tests pass in the unit suite;
  GitHub Actions runs `--quick` on push/PR and the full matrix on manual dispatch or schedule.
- Installer: temporary-home round trips, original asset/service restoration, and
  literal paths containing shell-special characters pass (`tests/test_install.sh`).
- Extension logic: 43 profile assertions (`tests/test_quick_profiles.js`), 363 gesture
  assertions across `tests/test_gestures.js` (42 desktop drag, 23 app drag, 32 gesture session,
  266 gesture logic), 23 standalone assertions in `tests/test_app_drag.js`, and ES module syntax
  checks across all 10 extension files pass.
- Focused Shell QA covers reversible desktop travel, app switching, overview handoff,
  reduced motion and media dispatch, using existing window actors.
- Extension schemas validate (`glib-compile-schemas --strict --dry-run`); packaging metadata and
  version-sync unit tests pass locally, while `desktop-file-validate` and `appstreamcli` run in
  the CI `packaging` job.
- Python AST/tokenize parity checks for Ruff `select = ["E", "F"]` (`src`, `tests`, `tools`) pass
  locally with zero issues; `ruff` and `shellcheck` were not installed in the audit sandbox and are
  enforced on GitHub Actions alongside Python 3.13 pure-Python tests and release build provenance
  attestation.
- Tracked-text scan found no real home paths or obvious credential patterns.

Update results use installed versions read before and after a transaction. Firmware
is reported as pending a restart only with fwupd staging evidence. Dependency
installation rejects held packages, removal plans and a plan changed since
confirmation. Cancellation is checked before submitting package changes, before
firmware flashing, and between firmware devices. An active flash finishes safely.

Real authenticated PackageKit/fwupd installations, physical shortcut mappings,
physical gesture feel and support on other laptop models remain unverified.
The earlier single-machine footprint measurements are documented in PERFORMANCE.md;
they are not a controlled comparison with another application.

## Cutting a release

Before tagging, move shipped changes from `[Unreleased]` to a dated version section in
`CHANGELOG.md` and update the AppStream `<releases>` version/date to the publication date.
Verify version sync (`pyproject.toml`, `src/asusius/__init__.py`, `packaging/arch/PKGBUILD`,
`data/Asusius.metainfo.xml`, `CHANGELOG.md`), desktop/AppStream validation, `python3 -m build`,
unit tests, `ruff`, `shellcheck`, `tests/test_install.sh`, and `gjs` extension tests on the exact
commit to be tagged (and make sure CI passes on GitHub before publishing).

Then create a new annotated tag (`git tag -a vX.Y.Z -m "Asusius vX.Y.Z"`), build the clean
deterministic source archive:

    rm -rf dist
    python3 tools/release_archive.py --tag vX.Y.Z --out dist
    (cd dist && sha256sum -c SHA256SUMS)

Publish the `dist/asusius-X.Y.Z.tar.gz` and `dist/SHA256SUMS` assets via the `Release archives`
workflow (or upload directly), then pin `sha256sums` in `packaging/arch/PKGBUILD` before updating
the AUR package. Keep new development under `[Unreleased]`; published tags and archives remain
immutable.

### GitHub maintenance notes (resolved in `v0.2.1`)

- **`v0.2.0` GitHub Release (`7b0caa3`)**: superseded by **`v0.2.1`**, with `v0.2.0`
  release notes updated to reference `asusius-0.2.0.tar.gz` and its matching SHA-256.
- **GitHub Issue `#3`**: updated to instruct testers to run `asusius capabilities --json`.

### Audit & CI hardening notes (`v0.3.0`, 2026-11-21)

- Fixed telemetry unit churn (`fmt.sticky_bytes`, `fmt.sticky_rate`, pre-threshold rounding),
  fan-key collisions in `sensors.py`, `CoreGrid` `°F` formatting, and Battery range-chip toggle guard
  plus keyboard history navigation.
- Hardened D-Bus connect timeouts, update lockfile permissions (`0o700`/`0o600`), scheduler lock
  separation, read-only `dry_run` write validation, sysfs/proc/NVML/MCP edge cases, UI parked-page
  state preservation, and GNOME 49 Shell feature detection.
- Upgraded CI/release workflows (`concurrency`, Python 3.13 pure-Python leg, PR/push `--quick` UI QA
  gate, pre-release Ruff + unit test gates, and build provenance attestation) and expanded
  `[tool.ruff.lint]` to `select = ["E", "F"]` across `src`, `tests`, and `tools`.
