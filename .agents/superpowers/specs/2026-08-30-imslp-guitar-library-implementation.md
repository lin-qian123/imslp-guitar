# IMSLP Pure-Guitar Library Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, migrate, populate, and verify a resumable offline IMSLP library for the approved 29 pure-guitar categories at `/Volumes/PHILIPS/programs/muse-cache/imslp`.

**Architecture:** A Python 3.12 CLI freezes IMSLP metadata into immutable run snapshots, applies strict original/arrangement extraction rules, stores verified PDFs once by SHA-256, materializes category-local hardlinks or relative symlinks, and renders self-contained `file://` catalogs. Existing `for3guitars` data is imported through a journaled staging migration; all network, migration, and download operations are resumable and produce machine-readable evidence.

**Tech Stack:** Python 3.12, standard library (`argparse`, `dataclasses`, `hashlib`, `html`, `json`, `pathlib`, `urllib`), `pypdf`, `pytest`, Node.js for executing the pure catalog-filter JavaScript in tests, static HTML/CSS/JavaScript, JSON/CSV/Markdown.

---

## Execution rules

- Run development in a dedicated Git worktree created with `@using-git-worktrees`; use temporary directories in tests and do not point development tests at the production score tree.
- Execute implementation tasks with `@subagent-driven-development` because subagents are available. Use `@test-driven-development` for every code task and `@verification-before-completion` before each chunk or rollout stage is declared complete.
- Run project commands with `python`, never `python3`.
- Keep all source and test files in Git. Keep PDFs, caches, `.part` files, generated catalogs, runtime metadata, logs, backups, quarantine, and migration staging out of Git.
- Never broaden `config/categories.json` from discovery output. A changed allowlist requires explicit user approval.
- Do not rename `for3guitars`, download PDFs, or write to `objects/` until Chunk 4 reaches the corresponding operational step.
- Use one focused commit per task. Before every commit run the exact targeted tests and `git diff --check`.

## Planned file map

| Path | Responsibility |
|---|---|
| `pyproject.toml` | Python package metadata, console entry point, test dependencies and pytest settings |
| `config/categories.json` | Versioned, approved allowlist and exact anchored heading/instrumentation patterns |
| `scripts/library.py` | Thin executable wrapper around the package CLI |
| `scripts/imslp_library/config.py` | Load and validate allowlist; compile exact patterns |
| `scripts/imslp_library/enums.py` | Shared string enums; starts with `CategoryKind`, then gains run/download/storage enums |
| `scripts/imslp_library/models.py` | Enums and dataclasses for categories, works, files, memberships, snapshots and statuses |
| `scripts/imslp_library/jsonio.py` | Deterministic JSON serialization and atomic/fsynced writes |
| `scripts/imslp_library/paths.py` | NFC-safe path mapping, collision suffixes and URL encoding |
| `scripts/imslp_library/headings.py` | Parse the `FILES` region into a hierarchical heading tree |
| `scripts/imslp_library/extractor.py` | Strict original/arrangement/work-level selection with evidence |
| `scripts/imslp_library/client.py` | IMSLP API/page transport, bounded retry, cache and response classification |
| `scripts/imslp_library/snapshot.py` | Freeze category members, revisions, file metadata and config hash for a run |
| `scripts/imslp_library/discovery.py` | Read-only guitar-category discovery and allowlist drift report |
| `scripts/imslp_library/downloader.py` | Resumable `.part` download, access-state classification and PDF validation |
| `scripts/imslp_library/storage.py` | Immutable SHA-256 object insertion, conflict quarantine and object manifests (target: ≤250 lines) |
| `scripts/imslp_library/materialize.py` | Link-capability probe and category-local hardlink/relative-symlink materialization (target: ≤220 lines) |
| `scripts/imslp_library/treehash.py` | Canonical logical-tree identities for staging, activation, archives and crash recovery |
| `scripts/imslp_library/legacy_audit.py` | Read-only legacy integrity, strict re-extraction and exclusion/review reports |
| `scripts/imslp_library/archive.py` | Preflight, verified read-only backup and independent archive manifests |
| `scripts/imslp_library/staging.py` | Idempotent eligible-object import and staging metadata/category paths |
| `scripts/imslp_library/migration.py` | Durable cutover journal, exact-path reconciliation, activation and rollback only |
| `scripts/imslp_library/translations.py` | Composer/title translation data, overrides and explicit fallback labels |
| `scripts/imslp_library/selection.py` | Run-bound category selectors and deterministic pilot/wave presets |
| `scripts/imslp_library/render.py` | Root/category HTML, Markdown, CSV and JSON output |
| `scripts/imslp_library/assets/catalog.js` | Pure search/filter function and DOM adapter; inlined into every generated HTML page |
| `scripts/imslp_library/verify.py` | Cross-layer invariants and machine-readable verification reports |
| `scripts/imslp_library/capacity.py` | Frozen-run capacity calculation, size overrides and persisted download gate |
| `scripts/imslp_library/workflows.py` | Command-level orchestration for doctor, extract, capacity, migration, rendering, verification and status |
| `scripts/imslp_library/cli.py` | Command definitions and orchestration; no domain logic |
| `tests/basic_helpers.py` | Model-free fixture readers/builders, including a pypdf-generated minimal PDF |
| `tests/model_helpers.py` | Fully valid typed model factories created after `models.py` |
| `tests/network_helpers.py` | Fake transport/response/clock and deterministic API/HTTP fixtures |
| `tests/fixtures/` | Minimal wikitext, API, HTML-block and legacy-manifest regression fixtures |
| `tests/test_*.py` | Unit, integration and failure-injection coverage corresponding to the modules above |

Public interfaces must remain small:

```python
load_allowlist(path: Path) -> LibraryConfig
parse_heading_tree(wikitext: str) -> HeadingTree
extract_memberships(page: FrozenPage, wikitext: str, category: CategoryRule) -> ExtractionResult
freeze_snapshot(root: Path, run_id: str, config: LibraryConfig, client: ImslpClient, clock: Clock) -> RunSnapshot
download_score(root: Path, run_id: str, target: DownloadTarget, transport: Transport, clock: Clock) -> DownloadResult
download_batch(root: Path, run_id: str, targets: tuple[DownloadTarget, ...], transport: Transport, clock: Clock) -> tuple[DownloadBatchResult, ...]
store_verified_pdf(root: Path, source: Path, score: ScoreFile, run_id: str) -> StoredObject
materialize_membership(root: Path, membership: Membership, stored: StoredObject) -> MaterializationResult
build_category_selection(root: Path, run_id: str, category_name: str | None = None, categories_file: Path | None = None, all_approved: bool = False) -> CategorySelection
build_library_dataset(metadata_root: Path, run_id: str, selection: CategorySelection, clock: Clock) -> LibraryDataset
render_library(output_root: Path, dataset: LibraryDataset, layout: str = "library_root") -> RenderSummary
verify_library(root: Path, run_id: str, request: VerificationRequest) -> VerificationArtifact
```

## Chunk 1: Deterministic core and strict score selection

### Task 1: Scaffold the package and approved category allowlist

**Files:**
- Create: `pyproject.toml`
- Create: `config/categories.json`
- Create: `scripts/imslp_library/__init__.py`
- Create: `scripts/imslp_library/enums.py`
- Create: `scripts/imslp_library/config.py`
- Create: `tests/basic_helpers.py`
- Create: `tests/test_config.py`
- Modify: `.gitignore`

- [ ] **Step 1: Add package metadata and install the development environment**

  Configure `pyproject.toml` with Python `>=3.12`, runtime dependency `pypdf>=6.8,<7`, optional test dependency `pytest>=9,<10`, package discovery under `scripts/`, and pytest `pythonpath = ["scripts", "."]`. Do not declare the console entry point or create `scripts/library.py` until Task 13 creates the real `cli.py`.

  Run: `python -m pip install -e '.[test]'`

  Expected: editable install succeeds and `python -c 'import pytest, pypdf'` exits `0`.

- [ ] **Step 2: Add generic helpers that do not import future models**

  Create `tests/basic_helpers.py` with this executable content (plus imports shown):

  ```python
  import json
  from pathlib import Path

  from pypdf import PdfWriter

  ROOT = Path(__file__).resolve().parents[1]

  def write_json(path: Path, payload: object) -> Path:
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
      return path

  def load_text_fixture(relative: str) -> str:
      return (ROOT / "tests/fixtures" / relative).read_text(encoding="utf-8")

  def make_file_template(filename: str, file_id: str = "1") -> str:
      return "\n".join(("{{#fte:imslpfile", f"|File Name 1={filename}", f"|File ID={file_id}", "}}"))

  def make_wikitext(files_body: str, instrumentation: str = "") -> str:
      return "\n".join(("| *****FILES*****", files_body, "| *****WORK INFO*****", f"|Instrumentation={instrumentation}"))

  def write_minimal_pdf(path: Path, *, width: int = 72, height: int = 72) -> Path:
      path.parent.mkdir(parents=True, exist_ok=True)
      writer = PdfWriter()
      writer.add_blank_page(width=width, height=height)
      with path.open("wb") as handle:
          writer.write(handle)
      return path
  ```

- [ ] **Step 3: Write the failing allowlist tests**

  In `tests/test_config.py`, define `EXPECTED_CATEGORY_NAMES` as the literal set of the 29 names in the canonical table in Step 4. Build invalid payloads from `json.loads((ROOT / "config/categories.json").read_text())`, not undefined helper factories. Assert:

  ```python
  import json
  from pathlib import Path

  import pytest

  from imslp_library.config import ConfigError, load_allowlist
  from tests.basic_helpers import ROOT, write_json

  def test_approved_allowlist_is_exact_and_nonempty():
      config = load_allowlist(ROOT / "config/categories.json")
      assert config.version == "2026-08-30.1"
      assert config.approved_at == "2026-08-30"
      assert len(config.categories) == 29
      assert {rule.name for rule in config.categories} == EXPECTED_CATEGORY_NAMES
      assert all(rule.name.startswith("For ") for rule in config.categories)

  def test_forbidden_or_unanchored_rules_are_rejected(tmp_path):
      payload = json.loads((ROOT / "config/categories.json").read_text())
      payload["categories"][0]["name"] = "For electric guitar"
      write_json(tmp_path / "categories.json", payload)
      with pytest.raises(ConfigError, match="excluded instrument"):
          load_allowlist(tmp_path / "categories.json")
  ```

- [ ] **Step 4: Run the tests and confirm the expected failure**

  Run: `python -m pytest tests/test_config.py -q`

  Expected: collection or import failure because `imslp_library.config` does not exist.

- [ ] **Step 5: Write the complete 29-entry allowlist from this canonical table**

  Top-level fields are `schema_version: 1`, `version: "2026-08-30.1"`, `approved_at: "2026-08-30"`, and `categories`. Every category has `name`, `url`, `kind`, `display_group`, `guitar_count`, `extended_strings`, `instrumentation_patterns`, `heading_patterns`, and the literal `annotation_reject_tokens` list below. The exact URL is `https://imslp.org/wiki/Category:` plus the exact name with spaces replaced by underscores; validation checks this equality.

  `P(n)` below means exact normalized pattern `^<n> guitars$`; `H(n)` means `^for <n> guitars(?: \((?P<annotation>[^()]*)\))?$`. `P(1)` is `^guitar$`; `H(1)` is `^for guitar(?: \((?P<annotation>[^()]*)\))?$`. The matcher allows punctuation in human-name annotations, then rejects the annotation only if tokenization finds an instrument term. Use the following literal rows—no inferred extra rows:

  | Name | Kind | Group | Count | Extended strings | Instrumentation | Heading |
  |---|---|---|---|---:|---|---|
  | `For guitar` | original | solo | 1 | null | `P(1)` | none |
  | `For guitar (arr)` | arrangement | solo | 1 | null | `P(1)` | `H(1)` |
  | `For 2 guitars` | original | duo | 2 | null | `P(2)` | none |
  | `For 2 guitars (arr)` | arrangement | duo | 2 | null | `P(2)` | `H(2)` |
  | `For 3 guitars` | original | trio | 3 | null | `P(3)` | none |
  | `For 3 guitars (arr)` | arrangement | trio | 3 | null | `P(3)` | `H(3)` |
  | `For 4 guitars` | original | quartet | 4 | null | `P(4)` | none |
  | `For 4 guitars (arr)` | arrangement | quartet | 4 | null | `P(4)` | `H(4)` |
  | `For 5 guitars` | original | five_guitars | 5 | null | `P(5)` | none |
  | `For 5 guitars (arr)` | arrangement | five_guitars | 5 | null | `P(5)` | `H(5)` |
  | `For 6 guitars` | original | six_guitars | 6 | null | `P(6)` | none |
  | `For 6 guitars (arr)` | arrangement | six_guitars | 6 | null | `P(6)` | `H(6)` |
  | `For 7 guitars` | original | seven_guitars | 7 | null | `P(7)` | none |
  | `For 8 guitars` | original | eight_guitars | 8 | null | `P(8)` | none |
  | `For 8 guitars (arr)` | arrangement | eight_guitars | 8 | null | `P(8)` | `H(8)` |
  | `For 9 guitars` | original | nine_guitars | 9 | null | `P(9)` | none |
  | `For 12 guitars` | original | twelve_guitars | 12 | null | `P(12)` | none |
  | `For 12 guitars (arr)` | arrangement | twelve_guitars | 12 | null | `P(12)` | `H(12)` |
  | `For 16 guitars` | original | sixteen_guitars | 16 | null | `P(16)` | none |
  | `For 6 string guitar (arr)` | arrangement | extended_solo | 1 | 6 | `^6 string guitar$` | `^for 6 string guitar(?: \((?P<annotation>[^()]*)\))?$` |
  | `For 7 string guitar (arr)` | arrangement | extended_solo | 1 | 7 | `^7 string guitar$` | `^for 7 string guitar(?: \((?P<annotation>[^()]*)\))?$` |
  | `For 7-string guitar (arr)` | arrangement | extended_solo | 1 | 7 | `^7-string guitar$` | `^for 7-string guitar(?: \((?P<annotation>[^()]*)\))?$` |
  | `For 8 string guitar (arr)` | arrangement | extended_solo | 1 | 8 | `^8 string guitar$` | `^for 8 string guitar(?: \((?P<annotation>[^()]*)\))?$` |
  | `For 8-string guitar (arr)` | arrangement | extended_solo | 1 | 8 | `^8-string guitar$` | `^for 8-string guitar(?: \((?P<annotation>[^()]*)\))?$` |
  | `For 10 string guitar (arr)` | arrangement | extended_solo | 1 | 10 | `^10 string guitar$` | `^for 10 string guitar(?: \((?P<annotation>[^()]*)\))?$` |
  | `For 10-string guitar (arr)` | arrangement | extended_solo | 1 | 10 | `^10-string guitar$` | `^for 10-string guitar(?: \((?P<annotation>[^()]*)\))?$` |
  | `For 2 and 3 guitars (arr)` | arrangement | flexible_ensemble | [2, 3] | null | `^2 and 3 guitars$` | `^for 2 and 3 guitars(?: \((?P<annotation>[^()]*)\))?$` |
  | `For guitar ensemble (arr)` | arrangement | ensemble | ensemble | null | `^guitar ensemble$` | `^for guitar ensemble(?: \((?P<annotation>[^()]*)\))?$` |
  | `For guitar orchestra (arr)` | arrangement | orchestra | ensemble | null | `^guitar orchestra$` | `^for guitar orchestra(?: \((?P<annotation>[^()]*)\))?$` |

  Outside the optional parentheses, full-match itself rejects commas, semicolons and added `and`, `with` or `or` text; the flexible category accepts only its literal `2 and 3` phrase. Inside `annotation`, tokenize Unicode words and reject this literal `annotation_reject_tokens` vocabulary: `accordion`, `bass`, `bassoon`, `brass`, `cello`, `clarinet`, `contrabass`, `double bass`, `drum`, `drums`, `electric`, `flute`, `guitar`, `harp`, `harpsichord`, `horn`, `keyboard`, `lute`, `mandolin`, `oboe`, `organ`, `percussion`, `piano`, `recorder`, `saxophone`, `strings`, `trombone`, `trumpet`, `tuba`, `ukulele`, `viola`, `violin`, `voice`, `voices`, `vocal`, `woodwind`. Add explicit passing tests for `(Smith, John)` and `(Höger, Anton)`, and rejecting tests for `(Smith, piano)`, `(organ)`, `(recorder)`, `(ukulele)` and `(drums)`.

- [ ] **Step 6: Write structural validation mutations**

  Parametrize `(mutation, error_match)` cases for duplicate name, removed `^`, removed `$`, extra unknown key, empty pattern list, invalid guitar count and invalid regex. Each mutation is a small function defined in `tests/test_config.py` that edits a deep copy of the real payload; the test writes it with `write_json` and asserts the stable `ConfigError` message.

- [ ] **Step 7: Write scope and category-kind validation mutations**

  Add independent mutations for URL/name mismatch, electric category name, bass category name, arrangement with empty headings, original with empty instrumentation and wrong `kind`. Assert distinct error codes `url_name_mismatch`, `excluded_instrument`, `missing_heading_patterns`, `missing_instrumentation_patterns` and `invalid_kind`. The checked-in config’s exact 29-name equality is enforced by `test_approved_allowlist_is_exact_and_nonempty`; the reusable loader does not hard-code the current set, so a future explicitly approved allowlist version remains possible.

- [ ] **Step 8: Implement the shared category enum and strict configuration validation**

  Create `CategoryKind(str, Enum)` with only `ORIGINAL="original"` and `ARRANGEMENT="arrangement"` in `enums.py`; `config.py` imports it. `config.py` must satisfy the mutation matrix and compile every canonical pattern. Return frozen dataclasses rather than raw dictionaries. Canonicalize the parsed JSON with sorted keys and compact separators before SHA-256 so file whitespace and key ordering do not affect the config hash.

- [ ] **Step 9: Extend ignore rules for generated runtime state**

  Ignore `.pytest_cache/`, `*.egg-info/`, root generated `index.html`, `catalog.csv`, `score_manifest.csv`, every generated `/For */` category tree, `objects/`, `backups/`, `quarantine/`, `_migration/`, `metadata/runs/`, `metadata/migrations/`, `metadata/verification/`, `metadata/operations/`, runtime catalog/manifests/path maps/category drift and runtime translation-coverage outputs. Keep `config/`, `metadata/translations/*.json`, `metadata/overrides/**/*.json`, source/tests/docs and the three project Markdown files tracked. Add an ignore-policy test that creates representative generated root/category/runtime paths and tracked translation/override paths, then asserts `git check-ignore` matches only the generated set.

- [ ] **Step 10: Run tests and commit**

  Run: `python -m pytest tests/test_config.py -q`

  Expected: all configuration tests pass and report 29 categories.

  Run: `git diff --check`

  Commit:

  ```bash
  git add pyproject.toml config/categories.json scripts/imslp_library tests/basic_helpers.py tests/test_config.py .gitignore
  git commit -m "feat: define IMSLP guitar category allowlist"
  ```

### Task 2: Add typed models and crash-safe JSON persistence

**Files:**
- Modify: `scripts/imslp_library/enums.py`
- Create: `scripts/imslp_library/models.py`
- Create: `scripts/imslp_library/jsonio.py`
- Create: `tests/model_helpers.py`
- Create: `tests/test_models.py`
- Create: `tests/test_jsonio.py`

- [ ] **Step 1: Lock the enum vocabulary in failing tests**

  Confirm the existing `CategoryKind={original,arrangement}` and define expected literal values for `StorageMethod={hardlink,relative_symlink}`, `SelectionReason={exact_original_instrumentation,exact_arrangement_heading,work_level_exact_instrumentation}`, `RunStatus={snapshot_incomplete,snapshot_complete,in_progress,paused,complete,failed}`, `AttemptPhase={started,streaming,finished}`, `ReviewStatus={not_required,pending,resolved}`, `IssueSeverity={info,warning,error}` and the download statuses `not_started`, `retryable`, `human_verification_required`, `membership_wait_pending`, `copyright_restricted`, `region_restricted`, `membership_required`, `login_required`, `commercial_only`, `deleted`, `manual_review`, `downloaded_verified`, `source_override_verified`. Assert unknown values raise `ValueError`.

- [ ] **Step 2: Lock these exact frozen dataclass schemas in constructor tests**

  All dataclasses use `frozen=True, slots=True`; fields without `| None` are required and collection fields are tuples:

  | Model | Exact fields |
  |---|---|
  | `FrozenPage` | `page_id:int`, `revision_id:int`, `page_title:str`, `category_names:tuple[str,...]`, `wikitext_path:str`, `wikitext_sha256:str` |
  | `Work` | `work_id:str`, `page_id:int`, `revision_id:int`, `page_title:str`, `title_en:str`, `composer_en:str`, `imslp_url:str` |
  | `ScoreFile` | `source_id:str`, `file_id:str`, `page_id:int`, `page_revision_id:int`, `filename:str`, `source_url:str`, `expected_size:int|None`, `sha1_imslp:str|None`, `source_hash_missing:bool`, `mime:str|None`, `copyright_label:str|None`, `sha256:str|None`, `object_path:str|None` |
  | `SelectionEvidence` | `heading_raw:str|None`, `heading_normalized:str|None`, `heading_ancestry:tuple[str,...]`, `instrumentation_raw:str|None`, `instrumentation_normalized:str|None`, `branch:str`, `reason_detail:str` |
  | `Membership` | `membership_id:str`, `category_name:str`, `work_id:str`, `source_id:str`, `selection_reason:SelectionReason`, `evidence:SelectionEvidence`, `planned_local_path:str`, `local_path:str|None`, `storage_method:StorageMethod|None`, `active:bool` |
  | `ExtractionDecision` | `source_id:str|None`, `filename:str`, `disposition:str` (`selected|excluded|manual_review`), `reason_code:str`, `selection_reason:SelectionReason|None`, `evidence:SelectionEvidence` |
  | `ExtractionResult` | `page_id:int`, `revision_id:int`, `category_name:str`, `decisions:tuple[ExtractionDecision,...]` plus computed `selected`, `excluded`, `manual_review` properties |
  | `ExtractionReview` | `run_id:str`, `category_name:str`, `page_id:int`, `revision_id:int`, `source_id:str|None`, `decision:str` (`exclude` only), `reason:str`, `extraction_decision_sha256:str`, `reviewed_at:datetime` |
  | `CategorySnapshot` | `name:str`, `member_page_ids:tuple[int,...]`, `member_count:int` |
  | `RunSnapshot` | `schema_version:int`, `run_id:str`, `config_version:str`, `config_sha256:str`, `snapshot_started_at:datetime`, `snapshot_completed_at:datetime|None`, `status:RunStatus`, `categories:tuple[CategorySnapshot,...]`, `pages:tuple[FrozenPage,...]`, `score_files:tuple[ScoreFile,...]` |
  | `RunState` | `schema_version:int`, `run_id:str`, `snapshot_path:str`, `snapshot_sha256:str|None`, `start_drift_report_path:str|None`, `start_drift_report_sha256:str|None`, `end_drift_report_path:str|None`, `end_drift_report_sha256:str|None`, `download_attempt_manifest_path:str|None`, `updated_at:datetime` |
  | `DownloadTarget` | `category_name:str`, `membership_id:str`, `score:ScoreFile` |
  | `DownloadAttempt` | `run_id:str`, `attempt_number:int`, `category_names:tuple[str,...]`, `membership_ids:tuple[str,...]`, `source_id:str`, `phase:AttemptPhase`, `status:DownloadStatus`, `review_status:ReviewStatus`, `started_at:datetime`, `completed_at:datetime|None`, `http_status:int|None`, `evidence_path:str|None`, `evidence_sha256:str|None`, `part_path:str|None`, `bytes_written:int`, `retry_after:datetime|None`, `detail_code:str` |
  | `DownloadBatchResult` | `source_id:str`, `category_names:tuple[str,...]`, `membership_ids:tuple[str,...]`, `result:DownloadResult` |
  | `SourceHashReview` | `source_id:str`, `page_revision_id:int`, `object_sha256:str`, `decision:str` (`accept_structural_without_source_hash|reject`), `reviewer_note:str`, `reviewed_at:datetime` |
  | `DownloadResult` | `source_id:str`, `status:DownloadStatus`, `review_status:ReviewStatus`, `review_evidence_path:str|None`, `attempted_at:datetime`, `retry_after:datetime|None`, `http_status:int|None`, `evidence_path:str|None`, `detail:str`, `size:int|None`, `sha1:str|None`, `sha256:str|None`, `source_hash_missing:bool` |
  | `StoredObject` | `sha256:str`, `size:int`, `object_path:str`, `sha1_imslp:str|None`, `verified_at:datetime` |
  | `MaterializationResult` | `membership_id:str`, `local_path:str`, `storage_method:StorageMethod`, `sha256:str` |
  | `VerificationIssue` | `code:str`, `severity:IssueSeverity`, `stable_ids:tuple[str,...]`, `evidence:dict[str,object]` |
  | `VerificationReport` | `schema_version:int`, `run_id:str`, `scope:str`, `scope_key:str`, `verified_at:datetime`, `facts:dict[str,object]`, `issues:tuple[VerificationIssue,...]`, `complete:bool`, `report_sha256:str` |

  `CategoryRule` remains the frozen config model in `config.py` and uses `CategoryKind`; it is not duplicated in `models.py`.

- [ ] **Step 3: Add executable source-model factories**

  Begin `tests/model_helpers.py` with the imports and exact factory pattern below. Implement `make_frozen_page`, `make_work`, `make_score`, and `make_evidence` using the shown literal defaults; `_build` is the only override mechanism.

  ```python
  import hashlib
  from datetime import datetime, timezone

  from imslp_library.enums import (
      AttemptPhase, DownloadStatus, IssueSeverity, ReviewStatus, RunStatus,
      SelectionReason, StorageMethod,
  )
  from imslp_library.models import (
      CategorySnapshot, DownloadAttempt, DownloadBatchResult, DownloadResult, DownloadTarget,
      ExtractionDecision, ExtractionResult, ExtractionReview, FrozenPage, MaterializationResult,
      Membership, RunSnapshot, RunState, ScoreFile, SelectionEvidence, SourceHashReview,
      StoredObject, VerificationIssue, VerificationReport, Work,
  )

  NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
  SHA1 = "a" * 40
  SHA256 = "b" * 64

  def _build(cls, defaults: dict[str, object], overrides: dict[str, object]):
      return cls(**(defaults | overrides))

  def make_frozen_page(**overrides):
      return _build(FrozenPage, {
          "page_id": 101, "revision_id": 202,
          "page_title": "Fixture (Composer, Test)",
          "category_names": ("For 3 guitars (arr)",),
          "wikitext_path": "metadata/.cache/pages/101/202.wiki",
          "wikitext_sha256": SHA256,
      }, overrides)

  def make_work(**overrides):
      return _build(Work, {
          "work_id": "work:p101@r202", "page_id": 101, "revision_id": 202,
          "page_title": "Fixture (Composer, Test)", "title_en": "Fixture",
          "composer_en": "Composer, Test",
          "imslp_url": "https://imslp.org/wiki/Fixture_(Composer,_Test)",
      }, overrides)

  def make_score(file_id="301", **overrides):
      return _build(ScoreFile, {
          "source_id": f"source:f{file_id}@r202", "file_id": str(file_id),
          "page_id": 101, "page_revision_id": 202, "filename": "score.pdf",
          "source_url": "https://imslp.org/files/score.pdf", "expected_size": 123,
          "sha1_imslp": SHA1, "source_hash_missing": False,
          "mime": "application/pdf", "copyright_label": "Public Domain",
          "sha256": None, "object_path": None,
      }, overrides)

  def make_evidence(**overrides):
      return _build(SelectionEvidence, {
          "heading_raw": "For 3 Guitars", "heading_normalized": "for 3 guitars",
          "heading_ancestry": ("Arrangements and Transcriptions", "For 3 Guitars"),
          "instrumentation_raw": "orchestra", "instrumentation_normalized": "orchestra",
          "branch": "Arrangements and Transcriptions", "reason_detail": "exact heading",
      }, overrides)
  ```

- [ ] **Step 4: Add executable state-model factories**

  Continue the same file with these exact defaults:

  ```python
  def make_membership(category="For 3 guitars (arr)", filename="score.pdf", **overrides):
      source_id = "source:f301@r202"
      category_sha10 = hashlib.sha256(category.encode("utf-8")).hexdigest()[:10]
      return _build(Membership, {
          "membership_id": f"membership:{category_sha10}:{source_id}",
          "category_name": category, "work_id": "work:p101@r202",
          "source_id": source_id,
          "selection_reason": SelectionReason.EXACT_ARRANGEMENT_HEADING,
          "evidence": make_evidence(),
          "planned_local_path": f"{category}/scores/Composer, Test/Fixture/{filename}",
          "local_path": None,
          "storage_method": None, "active": True,
      }, overrides)

  def make_download_result(**overrides):
      return _build(DownloadResult, {
          "source_id": "source:f301@r202",
          "status": DownloadStatus.DOWNLOADED_VERIFIED,
          "review_status": ReviewStatus.NOT_REQUIRED, "review_evidence_path": None,
          "attempted_at": NOW,
          "retry_after": None, "http_status": 200, "evidence_path": None,
          "detail": "verified", "size": 123, "sha1": SHA1,
          "sha256": SHA256, "source_hash_missing": False,
      }, overrides)

  def make_stored_object(**overrides):
      return _build(StoredObject, {
          "sha256": SHA256, "size": 123,
          "object_path": f"objects/{SHA256[:2]}/{SHA256}.pdf",
          "sha1_imslp": SHA1, "verified_at": NOW,
      }, overrides)
  ```

- [ ] **Step 5: Add executable extraction and run factories**

  Continue with:

  ```python
  def make_extraction_decision(**overrides):
      return _build(ExtractionDecision, {
          "source_id": "source:f301@r202", "filename": "score.pdf",
          "disposition": "selected", "reason_code": "exact_arrangement_heading",
          "selection_reason": SelectionReason.EXACT_ARRANGEMENT_HEADING,
          "evidence": make_evidence(),
      }, overrides)

  def make_extraction_result(**overrides):
      return _build(ExtractionResult, {
          "page_id": 101, "revision_id": 202,
          "category_name": "For 3 guitars (arr)",
          "decisions": (make_extraction_decision(),),
      }, overrides)

  def make_extraction_review(**overrides):
      return _build(ExtractionReview, {
          "run_id": "run-20260830T120000Z", "category_name": "For 3 guitars (arr)",
          "page_id": 101, "revision_id": 202, "source_id": "source:f301@r202",
          "decision": "exclude", "reason": "mixed instrumentation confirmed",
          "extraction_decision_sha256": SHA256, "reviewed_at": NOW,
      }, overrides)

  def make_category_snapshot(**overrides):
      return _build(CategorySnapshot, {
          "name": "For 3 guitars (arr)", "member_page_ids": (101,),
          "member_count": 1,
      }, overrides)

  def make_run_snapshot(**overrides):
      return _build(RunSnapshot, {
          "schema_version": 1, "run_id": "run-20260830T120000Z",
          "config_version": "2026-08-30.1", "config_sha256": SHA256,
          "snapshot_started_at": NOW, "snapshot_completed_at": NOW,
          "status": RunStatus.SNAPSHOT_COMPLETE,
          "categories": (make_category_snapshot(),),
          "pages": (make_frozen_page(),), "score_files": (make_score(),),
      }, overrides)

  def make_run_state(**overrides):
      return _build(RunState, {
          "schema_version": 1, "run_id": "run-20260830T120000Z",
          "snapshot_path": "metadata/runs/run-20260830T120000Z.json",
          "snapshot_sha256": SHA256,
          "start_drift_report_path": None, "start_drift_report_sha256": None,
          "end_drift_report_path": None, "end_drift_report_sha256": None,
          "download_attempt_manifest_path": None, "updated_at": NOW,
      }, overrides)
  ```

- [ ] **Step 6: Add executable materialization and verification factories**

  Finish the helper file with:

  ```python
  def make_download_target(**overrides):
      return _build(DownloadTarget, {
          "category_name": "For 3 guitars (arr)",
          "membership_id": make_membership().membership_id,
          "score": make_score(),
      }, overrides)

  def make_download_attempt(**overrides):
      return _build(DownloadAttempt, {
          "run_id": "run-20260830T120000Z", "attempt_number": 1,
          "category_names": ("For 3 guitars (arr)",),
          "membership_ids": (make_membership().membership_id,),
          "source_id": "source:f301@r202",
          "phase": AttemptPhase.FINISHED,
          "status": DownloadStatus.DOWNLOADED_VERIFIED,
          "review_status": ReviewStatus.NOT_REQUIRED,
          "started_at": NOW, "completed_at": NOW, "http_status": 200,
          "evidence_path": None, "evidence_sha256": None, "part_path": None,
          "bytes_written": 123, "retry_after": None, "detail_code": "verified",
      }, overrides)

  def make_download_batch_result(**overrides):
      return _build(DownloadBatchResult, {
          "source_id": "source:f301@r202",
          "category_names": ("For 3 guitars (arr)",),
          "membership_ids": (make_membership().membership_id,),
          "result": make_download_result(),
      }, overrides)

  def make_source_hash_review(**overrides):
      return _build(SourceHashReview, {
          "source_id": "source:f301@r202", "page_revision_id": 202,
          "object_sha256": SHA256,
          "decision": "accept_structural_without_source_hash",
          "reviewer_note": "PDF header, size, parseability and SHA-256 reviewed",
          "reviewed_at": NOW,
      }, overrides)

  def make_materialization_result(**overrides):
      return _build(MaterializationResult, {
          "membership_id": make_membership().membership_id,
          "local_path": "For 3 guitars (arr)/scores/Composer, Test/Fixture/score.pdf",
          "storage_method": StorageMethod.HARDLINK, "sha256": SHA256,
      }, overrides)

  def make_verification_issue(**overrides):
      return _build(VerificationIssue, {
          "code": "example_warning", "severity": IssueSeverity.WARNING,
          "stable_ids": ("source:f301@r202",), "evidence": {"detail": "fixture"},
      }, overrides)

  def make_verification_report(**overrides):
      return _build(VerificationReport, {
          "schema_version": 1, "run_id": "run-20260830T120000Z",
          "scope": "all_approved", "scope_key": "all",
          "verified_at": NOW, "facts": {"checked_memberships": 1},
          "issues": (make_verification_issue(),),
          "complete": False, "report_sha256": SHA256,
      }, overrides)
  ```

- [ ] **Step 7: Write failing field-invariant tests**

  Assert required IDs are nonempty, page/revision/size fields are nonnegative, SHA-1 is 40 lowercase hex when present, SHA-256 is 64 lowercase hex when present, datetimes are timezone-aware, `source_hash_missing` equals `(sha1_imslp is None)`, and `local_path/storage_method` are either both absent or both present. ExtractionReview is exclusion-only, has a nonblank reason, exact 64-hex decision digest and aware time; it can never force inclusion. `RunState.snapshot_path` is always nonempty; `snapshot_sha256` is absent only while the referenced snapshot is incomplete and is required once it is complete. Each start/end drift report path and SHA-256 is an all-or-none pair. For `DownloadAttempt`, require `attempt_number > 0`, `bytes_written >= 0`, equal nonempty `category_names`/`membership_ids` lengths with unique stable-sorted pairs, `phase in {started,streaming}` with `status=not_started` and `completed_at=None`, and `phase=finished` with a non-`not_started` status, aware `completed_at`, and `completed_at >= started_at`. `DownloadBatchResult` requires the same nonempty, equal-length, unique stable-sorted association pairs and matching `source_id == result.source_id`. SourceHashReview requires exact 64-hex object hash, nonblank reviewer note, aware review time and one allowed decision. VerificationReport facts/evidence must be recursively JSON-serializable with string keys and deterministic sorted-key serialization.

- [ ] **Step 8: Write failing round-trip and ordering tests**

  Parametrize all factories from Steps 3–6. Assert every model round-trips through tagged `to_dict`/`from_dict` and reserialization is byte-identical. Assert heading ancestry and extraction decisions preserve input order. Separately assert aggregate builders sort only categories, pages, score files and verification issues by the keys specified in Step 14.

- [ ] **Step 9: Write the atomic-write interruption test with all imports**

  ```python
  import os
  from unittest.mock import Mock

  import pytest

  from imslp_library.jsonio import atomic_write_json, read_json

  def test_atomic_write_preserves_previous_file_on_replace_failure(tmp_path, monkeypatch):
      target = tmp_path / "state.json"
      atomic_write_json(target, {"state": "old"})
      monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("injected")))
      with pytest.raises(OSError, match="injected"):
          atomic_write_json(target, {"state": "new"})
      assert read_json(target)["state"] == "old"
  ```

- [ ] **Step 10: Run the focused tests and confirm failure**

  Run: `python -m pytest tests/test_models.py tests/test_jsonio.py -q`

  Expected: import failures for the missing modules.

- [ ] **Step 11: Extend enums and implement source models/validation**

  Implement the existing `CategoryKind` plus `StorageMethod`, `SelectionReason`, `RunStatus`, `AttemptPhase`, `ReviewStatus`, `IssueSeverity` and `DownloadStatus`, then implement `FrozenPage`, `Work`, `ScoreFile`, `SelectionEvidence`, `Membership`, `ExtractionDecision`, `ExtractionResult` and `ExtractionReview` exactly as specified. Stable IDs are `work:p<page_id>@r<revision_id>`, `source:f<file_id>@r<page_revision_id>`, and `membership:<category-sha10>:<source_id>`; validate but do not regenerate IDs during deserialization.

- [ ] **Step 12: Run only source-model tests**

  Run: `python -m pytest tests/test_models.py -q -k 'enum or frozen_page or work or score or membership or extraction'`

  Expected: selected source-model tests pass; run/download/storage/report tests still fail.

- [ ] **Step 13: Implement run, download, storage and verification records**

  Implement `CategorySnapshot`, `RunSnapshot`, `RunState`, `DownloadTarget`, `DownloadAttempt`, `DownloadBatchResult`, `SourceHashReview`, `DownloadResult`, `StoredObject`, `MaterializationResult`, `VerificationIssue` and `VerificationReport` exactly as specified. Validation must reject naive datetimes, inconsistent attempt phase/completion, replayable review records and inconsistent hash/size/status fields.

- [ ] **Step 14: Implement deterministic tagged serialization**

  Serialize enums by value, datetimes as timezone-aware ISO 8601 strings, tuples as arrays, dataclasses with `model_type` and exact field keys, and dictionaries with sorted keys. Serialization preserves tuple order. Before model construction, aggregate builders explicitly sort only set-like collections: run categories by name, pages by `(page_id, revision_id)`, score files by `source_id`, and verification issues by `(severity.value, code, stable_ids)`; heading ancestry, extraction decisions and other semantic sequences preserve source order. Deserialize only known `model_type` values and reject unknown/missing/extra keys.

- [ ] **Step 15: Implement deterministic atomic JSON/model writes**

  `atomic_write_json` writes an ordinary mapping. `atomic_write_models(path, model_type, items, schema_version=1)` writes the envelope `{"schema_version":1,"model_type":"MembershipManifest","items":[...]}`; `read_models(path, expected_model_type)` validates the envelope and returns typed items. Schema-version downgrade protection applies to envelopes and mapping documents, never to a bare list. Write UTF-8 JSON with sorted keys, stable indentation and a final newline to a sibling `.tmp`, flush and `os.fsync` the file, `os.replace`, then `fsync` the parent directory. Delete only the temporary file created by the failed call.

- [ ] **Step 16: Run tests and commit**

  Run: `python -m pytest tests/test_models.py tests/test_jsonio.py -q`

  Expected: all model and atomic-write tests pass.

  Run: `git diff --check`

  Commit:

  ```bash
  git add scripts/imslp_library/enums.py scripts/imslp_library/models.py scripts/imslp_library/jsonio.py tests/model_helpers.py tests/test_models.py tests/test_jsonio.py
  git commit -m "feat: add typed manifests and atomic state writes"
  ```

### Task 3: Parse heading trees and enforce exact pure-guitar extraction

**Files:**
- Create: `scripts/imslp_library/headings.py`
- Create: `scripts/imslp_library/extractor.py`
- Create: `tests/test_headings.py`
- Create: `tests/test_extractor.py`
- Create: `tests/fixtures/wikitext/exact_three_guitars.wiki`
- Create: `tests/fixtures/wikitext/mixed_bass.wiki`
- Create: `tests/fixtures/wikitext/original_guitar.wiki`
- Create: `tests/fixtures/wikitext/work_level_exact.wiki`
- Create: `tests/fixtures/wikitext/work_level_mixed.wiki`
- Create: `tests/fixtures/wikitext/flexible_2_and_3.wiki`
- Create: `tests/fixtures/wikitext/nested_other_instrument.wiki`

- [ ] **Step 1: Create the three arrangement-branch regression fixtures**

  Use these exact `FILES` bodies with `make_wikitext`; write the resulting text as the named fixture with `apply_patch`:

  | Fixture | Instrumentation | Exact body between markers |
  |---|---|---|
  | `exact_three_guitars.wiki` | `orchestra` | `===Arrangements and Transcriptions===\n====For 3 Guitars (Smith, John)====\n<PDF template three-guitars.pdf/file 101>\n<template source.mscz/file 102>` |
  | `mixed_bass.wiki` | `orchestra` | `===Arrangements and Transcriptions===\n====For 3 Guitars and Double Bass or Bass Guitar (Rest)====\n<PDF template mixed-score.pdf/file 201>` |
  | `nested_other_instrument.wiki` | `orchestra` | `===Arrangements and Transcriptions===\n====For 3 Guitars====\n=====With Bass Guitar=====\n<PDF template child-bass.pdf/file 301>` |

  Replace each `<PDF template name/file id>` and `<template name/file id>` token with the exact output of `make_file_template(name, id)`; do not add other headings or fields.

- [ ] **Step 2: Create original, work-level, and flexible-category fixtures**

  Use these exact bodies:

  | Fixture | Instrumentation | Exact body between markers |
  |---|---|---|
  | `original_guitar.wiki` | `guitar` | `===Scores and Parts===\n<PDF template original.pdf/file 401>\n===Arrangements and Transcriptions===\n====For Piano====\n<PDF template piano.pdf/file 402>` |
  | `work_level_exact.wiki` | `3 guitars` | `===Scores and Parts===\n<PDF template work-level.pdf/file 501>` |
  | `work_level_mixed.wiki` | `3 guitars and double bass` | `===Scores and Parts===\n<PDF template mixed-work-level.pdf/file 502>` |
  | `flexible_2_and_3.wiki` | `orchestra` | `===Arrangements and Transcriptions===\n====For 2 and 3 Guitars (Doe, Jane)====\n<PDF template flexible.pdf/file 601>` |

  Each test-side page builder sets `page_id=101`, `revision_id=202`, `page_title="Fixture (Composer, Test)"`, `category_names=(category_name,)`, and a SHA-256/path matching the loaded fixture; fixture wikitext alone never implies membership.

- [ ] **Step 3: Write failing heading-tree tests**

  Assert the parser ignores content outside `FILES`→`WORK INFO`, retains punctuation and `and/or/with`, preserves raw and normalized headings, attaches each node to the correct ancestor, and separates file templates by branch.

- [ ] **Step 4: Write failing exact/mixed extraction tests with a declared helper**

  Define the helper with this exact content, so frozen metadata and page text remain separate:

  ```python
  import hashlib
  import re

  from imslp_library.config import load_allowlist
  from imslp_library.extractor import extract_memberships
  from tests.basic_helpers import ROOT, load_text_fixture
  from tests.model_helpers import make_frozen_page

  CONFIG = load_allowlist(ROOT / "config/categories.json")
  KEEP_INSTRUMENTATION = object()
  REMOVE_INSTRUMENTATION = object()

  def extract_fixture(filename, category_name, *, category_membership=True,
                      instrumentation_override=KEEP_INSTRUMENTATION):
      text = load_text_fixture(f"wikitext/{filename}")
      if instrumentation_override is REMOVE_INSTRUMENTATION:
          text = re.sub(r"(?m)^\|Instrumentation=.*\n?", "", text)
      elif instrumentation_override is not KEEP_INSTRUMENTATION:
          text = re.sub(r"(?m)^\|Instrumentation=.*$",
                        f"|Instrumentation={instrumentation_override}", text)
      page = make_frozen_page(
          category_names=(category_name,) if category_membership else (),
          wikitext_path=f"tests/fixtures/wikitext/{filename}",
          wikitext_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
      )
      rule = next(rule for rule in CONFIG.categories if rule.name == category_name)
      return extract_memberships(page, text, rule)
  ```

  Then assert:

  ```python
  def test_exact_arrangement_is_selected_but_mixed_prefix_is_rejected():
      exact = extract_fixture("exact_three_guitars.wiki", "For 3 guitars (arr)")
      mixed = extract_fixture("mixed_bass.wiki", "For 3 guitars (arr)")
      assert [item.filename for item in exact.selected] == ["three-guitars.pdf"]
      assert mixed.selected == []
      assert mixed.excluded[0].reason_code == "mixed_instrument_heading"

  def test_work_level_exception_requires_all_four_evidence_conditions():
      accepted = extract_fixture("work_level_exact.wiki", "For 3 guitars (arr)")
      rejected = extract_fixture("work_level_mixed.wiki", "For 3 guitars (arr)")
      assert accepted.selected[0].selection_reason.value == "work_level_exact_instrumentation"
      assert rejected.selected == []
      assert rejected.manual_review
  ```

- [ ] **Step 5: Add parametrized rule-family coverage**

  Define `ACCEPTED_EXAMPLES` as a literal dictionary covering every configured name. Values are `(instrumentation, heading_or_none)` and correspond one-to-one to the 29 table rows: standard rows use `guitar`/`For Guitar` or `<N> guitars`/`For <N> Guitars`; extended rows use their exact spaced or hyphenated spelling; flexible uses `2 and 3 guitars`/`For 2 and 3 Guitars`; ensemble/orchestra use `guitar ensemble`/`For Guitar Ensemble` and `guitar orchestra`/`For Guitar Orchestra`. Assert dictionary keys equal the config name set before parametrizing. For every arrangement also test `(Smith, John)` and `(Höger, Anton)`. For extended-string rules reject the opposite hyphen/space variant for that rule even if a separate configured category accepts it.

- [ ] **Step 6: Add the exact-instrumentation work-level mutation**

  Call once with `instrumentation_override="3 guitars and double bass"`, once with `instrumentation_override=""`, and once with `instrumentation_override=REMOVE_INSTRUMENTATION`. All yield zero selected; mixed yields `work_level_instrumentation_not_exact`, empty/missing yield `work_level_instrumentation_missing`, and all enter manual review.

- [ ] **Step 7: Add the no-target-arrangement-child mutation**

  Insert `===Arrangements and Transcriptions===\n====For 3 Guitars====\n<PDF template arranged.pdf/file 503>` into the exact work-level fixture. The work-level source must not be selected; reason is `work_level_target_heading_present`. The arranged file is evaluated normally and may be selected independently.

- [ ] **Step 8: Add the original-branch work-level mutation**

  Replace `===Scores and Parts===` independently with `===Source Files===`, `===Synthesized/MIDI===`, `===Audio===`, and `===Commercial===`. Each yields zero selected and the stable reason `branch_not_allowed`; the non-PDF attachment in the exact fixture yields `not_pdf`.

- [ ] **Step 9: Add the target-membership work-level mutation**

  Call `extract_fixture(..., category_membership=False)`. It must yield zero selected and reason `page_not_in_category`. Also pass an original category rule to an arrangement fixture and an arrangement rule to the original fixture; both must yield a kind/branch-specific exclusion rather than selection.

- [ ] **Step 10: Add annotation safety tests**

  Generate exact headings with `(Smith, John)` and `(Höger, Anton)` and assert selection. Generate `(Smith, piano)`, `(organ)`, `(recorder)`, `(ukulele)` and `(drums)` and assert zero selection with `annotation_contains_instrument`.

- [ ] **Step 11: Run focused tests and confirm failure**

  Run: `python -m pytest tests/test_headings.py tests/test_extractor.py -q`

  Expected: import failures for `headings` and `extractor`.

- [ ] **Step 12: Implement the heading tree only**

  Normalize headings with Unicode NFC, MediaWiki-markup removal, HTML entity decoding, whitespace folding and case folding while retaining punctuation and relationship tokens. Represent every node with raw text, normalized text, level, parent, children, source span and file-template chunks.

- [ ] **Step 13: Run heading tests before implementing extraction**

  Run: `python -m pytest tests/test_headings.py -q`

  Expected: heading-tree tests pass while extractor tests still fail.

- [ ] **Step 14: Implement page-membership and branch gates**

  Return `page_not_in_category`, `branch_not_allowed` and `not_pdf` decisions first; run the focused tests for those reasons before adding title matching.

- [ ] **Step 15: Implement base original/arrangement matching**

  Use category membership, category kind, branch and `fullmatch` against the configured rule. Parse only PDF file templates. Return explicit excluded/manual-review reasons rather than booleans.

- [ ] **Step 16: Implement annotation and mixed-descendant rejection**

  Use the literal annotation vocabulary and exact reason codes above. Reject mixed suffixes or mixed descendants before parsing their file templates.

- [ ] **Step 17: Implement work-level gates with structured evidence**

  Use `fullmatch`, never prefix `search`, against the configured rule. Reject mixed suffixes or mixed descendant headings before parsing files. For every selected/excluded/manual-review item record page ID, revision ID, category, raw/normalized heading ancestry, raw/normalized instrumentation, branch, filename and reason. Deduplicate only identical file IDs within the same page revision; never deduplicate merely by filename.

- [ ] **Step 18: Implement exclusion-only extraction review replay guards**

  `extract_run` may apply typed reviews only from `metadata/overrides/extraction_reviews/<run-id>/`. The filename is `<category-sha10>-p<page_id>-<source-id-or-page>.json`. Recompute the canonical SHA-256 of the exact manual-review `ExtractionDecision`; require matching run/category/page/revision/source/digest, `decision=exclude`, nonblank reason and aware time. A review changes only `manual_review→excluded` and preserves original evidence plus review reference; it can never select a file or override an exact mixed-instrument rejection. Test stale revision/digest/run, extra source, attempted inclusion and valid exclusion.

- [ ] **Step 19: Run regression tests and commit**

  Run: `python -m pytest tests/test_headings.py tests/test_extractor.py -q`

  Expected: all fixtures pass, including zero selected files from the mixed-bass regression.

  Run: `git diff --check`

  Commit:

  ```bash
  git add scripts/imslp_library/headings.py scripts/imslp_library/extractor.py tests/test_headings.py tests/test_extractor.py tests/fixtures/wikitext
  git commit -m "feat: enforce exact pure-guitar score extraction"
  ```

### Task 4: Implement deterministic paths and immutable object storage

**Files:**
- Create: `scripts/imslp_library/paths.py`
- Create: `scripts/imslp_library/storage.py`
- Create: `scripts/imslp_library/materialize.py`
- Create: `tests/test_paths.py`
- Create: `tests/test_storage.py`
- Create: `tests/test_materialize.py`

- [ ] **Step 1: Write failing normalization, byte-limit and collision tests**

  Import `build_path_map` and `PathSource` explicitly. Test NFC equivalence, slash/control replacement, trailing dot/space cleanup, empty names, a 180-byte component cap without splitting UTF-8, case-fold collisions, characters ` #%'`, Chinese characters, and per-component URL quoting. Use `A/B` and `A_B` as the cleaning collision pair:

  ```python
  def test_collision_suffix_is_independent_of_input_order(tmp_path):
      one = PathSource(kind="work", original="A/B", stable_id="p7")
      two = PathSource(kind="work", original="A_B", stable_id="p8")
      first = build_path_map([one, two])
      second = build_path_map([two, one])
      assert first == second
      assert first[one].mapped.endswith("__p7")
      assert first[two].mapped.endswith("__p8")

  def test_collision_created_by_truncation_gets_stable_suffixes():
      one = PathSource(kind="work", original="曲" * 80 + "甲", stable_id="p7")
      two = PathSource(kind="work", original="曲" * 80 + "乙", stable_id="p8")
      mapped = build_path_map([one, two])
      assert mapped[one].mapped != mapped[two].mapped
      assert mapped[one].mapped.endswith("__p7")
      assert mapped[two].mapped.endswith("__p8")
      assert all(len(item.mapped.encode("utf-8")) <= 180 for item in mapped.values())
  ```

  Stable suffixes are composer `__c<first-10-hex-of-SHA256(NFC original attribution)>`, work `__p<pageid>`, and file `__f<fileid>` before `.pdf`. Apply suffixes to every member of an actual cleaning/case-fold collision set, not ordinary names; output must not depend on traversal order.

- [ ] **Step 2: Write failing object-store and quarantine tests**

  Use `write_minimal_pdf` with different page widths to create distinct valid PDFs; do not commit PDF fixtures or add a `.gitignore` exception. Add these executable cores, with `json`, `os`, `pytest`, `imslp_library.storage as storage`, `write_minimal_pdf` and `make_score` imported:

  ```python
  def score_for_path(path, file_id):
      return make_score(
          file_id=file_id, expected_size=path.stat().st_size,
          sha1_imslp=None, source_hash_missing=True,
      )

  def test_distinct_valid_pdfs_create_distinct_objects(tmp_path):
      one = write_minimal_pdf(tmp_path / "one.pdf", width=72)
      two = write_minimal_pdf(tmp_path / "two.pdf", width=73)
      first = storage.store_verified_pdf(tmp_path, one, score_for_path(one, "301"), "r1")
      second = storage.store_verified_pdf(tmp_path, two, score_for_path(two, "302"), "r1")
      assert first.sha256 != second.sha256
      assert len(list((tmp_path / "objects").rglob("*.pdf"))) == 2

  def test_interruption_leaves_part_but_no_object(tmp_path, monkeypatch):
      source = write_minimal_pdf(tmp_path / "source.pdf")
      original_copy = storage._copy_to_part
      def fail_after_first_chunk(source_path, part_path):
          with source_path.open("rb") as src, part_path.open("wb") as dst:
              dst.write(src.read(32))
              dst.flush()
              os.fsync(dst.fileno())
          raise OSError("injected after part creation")
      monkeypatch.setattr(storage, "_copy_to_part", fail_after_first_chunk)
      with pytest.raises(OSError, match="injected after part creation"):
          storage.store_verified_pdf(tmp_path, source, score_for_path(source, "301"), "r1")
      parts = list((tmp_path / "objects").rglob("*.part"))
      assert len(parts) == 1
      assert not list((tmp_path / "objects").rglob("*.pdf"))
      evidence_path = tmp_path / "metadata/runs/r1-object-writes.json"
      interrupted = json.loads(evidence_path.read_text(encoding="utf-8"))["items"][-1]
      assert interrupted["status"] == "object_write_interrupted"
      assert interrupted["bytes_written"] == 32
      assert interrupted["part_path"] == parts[0].relative_to(tmp_path).as_posix()

      monkeypatch.setattr(storage, "_copy_to_part", original_copy)
      stored = storage.store_verified_pdf(
          tmp_path, source, score_for_path(source, "301"), "r1"
      )
      assert (tmp_path / stored.object_path).is_file()
      assert not list((tmp_path / "objects").rglob("*.part"))
      attempts = json.loads(evidence_path.read_text(encoding="utf-8"))["items"]
      assert [item["status"] for item in attempts] == [
          "object_write_interrupted", "object_write_complete",
      ]
  ```

  Separately assert identical bytes create one object, a corrupt pre-existing object moves to `quarantine/objects/<run-id>/`, its manifest records original path, quarantine path, reason, size and SHA-256, and successful rerun is idempotent.

- [ ] **Step 3: Write failing materialization and conflict tests**

  Assert hardlinks are preferred, only relative symlinks inside the library root are accepted as fallback, failure of both mechanisms raises `StorageCapabilityError` before any category path remains, a pre-existing path to the same object is idempotent, and a pre-existing path to different content is quarantined and blocks membership success. The test imports `write_minimal_pdf` from `tests.basic_helpers`, model factories from `tests.model_helpers`, and storage/materialization functions explicitly.

  ```python
  from dataclasses import replace

  from imslp_library.jsonio import atomic_write_models, read_models
  from imslp_library.materialize import materialize_membership
  from imslp_library.storage import hash_file_sha256, store_verified_pdf
  from tests.basic_helpers import write_minimal_pdf
  from tests.model_helpers import make_membership, make_score

  def test_one_object_can_have_two_category_paths(tmp_path):
      source = write_minimal_pdf(tmp_path / "source.pdf")
      stored = store_verified_pdf(tmp_path, source, score_for_path(source, "301"), run_id="r1")
      first = materialize_membership(tmp_path, make_membership("For guitar", "one.pdf"), stored)
      second = materialize_membership(tmp_path, make_membership("For guitar (arr)", "two.pdf"), stored)
      assert len(list((tmp_path / "objects").rglob("*.pdf"))) == 1
      assert hash_file_sha256(tmp_path / first.local_path) == stored.sha256
      assert hash_file_sha256(tmp_path / second.local_path) == stored.sha256
      assert {first.storage_method.value, second.storage_method.value} <= {"hardlink", "relative_symlink"}

  def test_materialization_method_survives_manifest_round_trip(tmp_path):
      source = write_minimal_pdf(tmp_path / "source.pdf")
      stored = store_verified_pdf(tmp_path, source, score_for_path(source, "301"), run_id="r1")
      pending = make_membership("For guitar", "one.pdf")
      result = materialize_membership(tmp_path, pending, stored)
      committed = replace(pending, local_path=result.local_path, storage_method=result.storage_method)
      manifest = tmp_path / "metadata/memberships.json"
      atomic_write_models(manifest, "MembershipManifest", [committed])
      assert read_models(manifest, "MembershipManifest") == [committed]
  ```

- [ ] **Step 4: Run tests and confirm failure**

  Run: `python -m pytest tests/test_paths.py tests/test_storage.py tests/test_materialize.py -q`

  Expected: import failures for the missing path, storage and materialization modules.

- [ ] **Step 5: Implement normalization and stable collision resolution**

  Normalize with NFC; replace `/`, NUL and every control character with `_`; strip trailing spaces/dots; use `Untitled` for empty output. Compute a preliminary unsuffixed component by UTF-8-safe truncation to 180 bytes, then form collision groups from that preliminary value after case folding; this catches both cleaning collisions and collisions created only by truncation. For every group of size greater than one, reserve suffix plus extension bytes, retruncate the readable stem, append the exact stable suffix before the extension, and assert the final component is at most 180 bytes. Persist every original→mapped component, source kind, stable ID and collision reason in `metadata/path_map.json`.

- [ ] **Step 6: Implement chunked SHA-256 and object addressing**

  Add `hash_file_sha256` and the pure `object_path_for_hash(root, sha256) -> Path`; run only their tests. Paths must be `objects/<first-two-hex>/<sha256>.pdf`.

- [ ] **Step 7: Implement new object insertion and idempotent reuse**

  Implement `_copy_to_part(source_path, part_path)` as the single injected copy boundary used by `store_verified_pdf`. Copy to a same-directory `.part`, validate PDF/hash, fsync, atomic replace and fsync the parent. If the correct object already exists, re-hash and return it without rewriting. On interruption atomically append an item to `metadata/runs/<run-id>-object-writes.json` using envelope `schema_version=1`, `model_type="ObjectWriteAttemptManifest"`; each item has `source_id`, `part_path`, `bytes_written`, `status` (`object_write_interrupted|object_write_complete`), `error`, and timezone-aware `attempted_at`. The interruption test must assert `object_write_interrupted`, the exact `.part` relative path and byte count `32`. After restoring `_copy_to_part`, rerun the same source/run ID: it must replace only that recorded part, create the verified object, remove the part, append `object_write_complete`, and leave both attempts in the evidence manifest.

- [ ] **Step 8: Implement corrupt-object quarantine**

  If the expected object path contains different bytes, atomically move it to `quarantine/objects/<run-id>/`, atomically append path/reason/size/SHA-256 to its independent manifest, then insert the verified source. Run only conflict/quarantine tests and assert the old bytes remain recoverable.

- [ ] **Step 9: Run all storage tests before link implementation**

  Run: `python -m pytest tests/test_storage.py -q`

  Expected: object insertion, quarantine, interruption and idempotency tests pass.

- [ ] **Step 10: Implement target-volume capability probing**

  Probe hardlink and relative-symlink behavior in a freshly created disposable directory under the target root; clean up only its exact recorded files/directory. Return the supported method or raise `StorageCapabilityError`; run capability tests.

- [ ] **Step 11: Implement hardlink/relative-symlink materialization**

  Materialize to the exact planned local path, prefer hardlink, use only a root-contained relative symlink fallback, return `MaterializationResult`, and treat an existing same-object path as idempotent. Run success/fallback/idempotency tests.

- [ ] **Step 12: Implement occupied-path quarantine and manifest persistence**

  On different-content conflict, move the occupied path into `quarantine/paths/<run-id>/`, append its independent manifest, and raise without committing membership success. Then implement and run the `dataclasses.replace` + atomic manifest round-trip test shown in Step 3.

- [ ] **Step 13: Run Chunk 1 verification and commit**

  Run:

  ```bash
  python -m pytest tests/test_config.py tests/test_models.py tests/test_jsonio.py tests/test_headings.py tests/test_extractor.py tests/test_paths.py tests/test_storage.py tests/test_materialize.py -q
  git diff --check
  ```

  Expected: all Chunk 1 tests pass; no test writes outside pytest temporary directories.

  Commit:

  ```bash
  git add scripts/imslp_library/paths.py scripts/imslp_library/storage.py scripts/imslp_library/materialize.py tests/test_paths.py tests/test_storage.py tests/test_materialize.py
  git commit -m "feat: add deterministic paths and content-addressed storage"
  ```

### Chunk 1 traceability gate

| Specification requirement | Required test evidence before Chunk 1 closes |
|---|---|
| Design §3 and §6.1 allowlist boundary | Exact 29-name equality, approval/version fields, URL derivation, anchored compiled patterns, forbidden category/pattern failures |
| Design §6.3 original selection | Category membership + exact instrumentation + original Scores/Parts branch; wrong/missing condition tests |
| Design §6.3 arrangement selection | Exact heading full-match, ancestry, arranger-parentheses rule, mixed suffix/descendant rejection, every heading family parametrized |
| Design §6.3 work-level exception | Four gates mutated independently and evidence-backed reason asserted |
| Design §6.4 deterministic paths | NFC, 180-byte cap, slash/control cleanup, case-fold/cleaning collision, stable suffix and order independence |
| Design §§5/6.4 object identity | SHA-256 single entity, conflict quarantine manifest, interrupted write, idempotent object reuse |
| Design §§5/6.5 category paths | hardlink preference, relative-symlink fallback, dual-capability block, occupied-path quarantine, returned/persisted storage method |

## Chunk 2: IMSLP snapshot, downloader, and recoverable legacy migration

### Task 5: Build an injectable IMSLP client and immutable run snapshots

**Files:**
- Create: `scripts/imslp_library/client.py`
- Create: `scripts/imslp_library/snapshot.py`
- Create: `tests/network_helpers.py`
- Create: `tests/test_client.py`
- Create: `tests/test_snapshot.py`
- Create: `tests/fixtures/api/categorymembers-page-1.json`
- Create: `tests/fixtures/api/categorymembers-page-2.json`
- Create: `tests/fixtures/api/revisions.json`
- Create: `tests/fixtures/api/imageinfo.json`

- [ ] **Step 1: Add executable fake network and clock primitives**

  `tests/network_helpers.py` defines frozen `FakeResponse(status:int, headers:dict[str,str], chunks:tuple[bytes|Exception,...], final_url:str)` with `read()`/`iter_bytes()` that raise queued mid-stream exceptions, `FakeClock(current:datetime)` with classmethod `fixed()` returning `2026-08-30T12:00:00+00:00`, `now()`, and `sleep(seconds)` that advances without blocking, and `FakeTransport(responses:list[FakeResponse|Exception])`. `FakeTransport.request(method, url, *, headers, timeout)` appends an immutable call record, pops one response/exception, and fails if the queue is empty. Helpers `json_response(payload, status=200, headers=None)`, `bytes_response(body, content_type, status=200, final_url="https://imslp.org/")`, and `stream_response(chunks, ...)` create responses. No helper imports production client code.

- [ ] **Step 2: Write exact paginated API fixtures and client tests**

  Page 1 contains member page `101` and modern `continue.cmcontinue="next"`; page 2 contains page `102` and no continuation. Revision fixture maps page 101→revision 201 and 102→202. Imageinfo fixture contains file ID `301`, `size=123`, MIME `application/pdf`, SHA-1 of 40 `a` characters and an approved IMSLP URL. Tests assert request 2 uses `cmcontinue=next`, legacy `query-continue` is also accepted, `Accept-Encoding: identity` and the project user-agent are present, permanent 404 is attempted once, and timeout/429/503 stop after the configured attempt cap using `FakeClock`.

- [ ] **Step 3: Write snapshot lifecycle and resume tests**

  Assert `freeze_snapshot` atomically creates `metadata/runs/<run-id>.json` with `snapshot_incomplete` and `metadata/runs/<run-id>-state.json` before the first request; atomically checkpoints after each completed category and exact-revision batch; a crash after the first category leaves readable incomplete state; restart with the same run ID/config hash skips completed work; a different config hash refuses resume. Only after all 29 categories, exact revisions and file metadata are present does one final atomic transition set `snapshot_complete`/`snapshot_completed_at`; then compute/store snapshot SHA-256 in RunState and refuse any further mutation of the snapshot file.

- [ ] **Step 4: Write exact-revision drift and corrupt-cache tests**

  In pass 1 return page 101 revision 201, then make the fake “current page” revision 999. Assert pass 2 requests `revids=201`, stores only `metadata/.cache/pages/101/201.wiki`, and records revision 201. Precreate that cache path with bytes whose SHA-256 differs from the checkpoint; assert it is moved to `quarantine/cache/<run-id>/101/201.wiki` and registered in `quarantine/manifests/cache-<run-id>.json` with envelope `CacheQuarantineManifest` and item fields `page_id,revision_id,source_path,quarantine_path,size,expected_sha256,actual_sha256,reason,quarantined_at`. Exact revision 201 is refetched. Assert manifest payload paths/sizes/SHA-256 exactly equal the quarantine tree, and make a second returned-content hash mismatch fail the snapshot rather than accepting current content.

- [ ] **Step 5: Write determinism tests**

  Feed identical records in different API order. With fixed `run_id` and `FakeClock`, canonical snapshot JSON must be byte-identical because categories/pages/files are sorted by the aggregate-builder keys from Chunk 1; semantic page data is unchanged.

- [ ] **Step 6: Run tests and confirm failure**

  Run: `python -m pytest tests/test_client.py tests/test_snapshot.py -q`

  Expected: import failures for the missing client and snapshot modules.

- [ ] **Step 7: Implement bounded IMSLP API access**

  Implement query encoding, continuation, approved host validation, explicit connect/read timeouts, identity encoding, project user-agent, capped backoff and injected clock/sleeper. Expose category members, category info, exact revision-by-ID, imageinfo and allcategories pagination; return typed values, not raw response dictionaries.

- [ ] **Step 8: Implement initial checkpoint and category-pass resume**

  Write the incomplete run before network access. Checkpoint completed categories atomically and derive pending categories from the recorded names. Refuse download or extraction while status is incomplete. Derive config hash from canonical JSON bytes.

- [ ] **Step 9: Implement exact-revision pass, cache validation and completion**

  Request every captured revision by `revids`, validate/cache its SHA-256, parse file metadata from only that text/revision, checkpoint fixed-size batches, and transition once to complete. Implement the exact external cache quarantine manifest and equality check from Step 4; an upstream current revision never substitutes for the captured ID.

- [ ] **Step 10: Run tests and commit**

  Run: `python -m pytest tests/test_client.py tests/test_snapshot.py -q`

  Expected: all tests pass without network access.

  Run: `git diff --check`

  Commit:

  ```bash
  git add scripts/imslp_library/client.py scripts/imslp_library/snapshot.py tests/network_helpers.py tests/test_client.py tests/test_snapshot.py tests/fixtures/api
  git commit -m "feat: freeze reproducible IMSLP run snapshots"
  ```

### Task 6: Add read-only category discovery and drift reporting

**Files:**
- Create: `scripts/imslp_library/discovery.py`
- Create: `tests/test_discovery.py`
- Create: `tests/fixtures/api/allcategories-guitar.json`

- [ ] **Step 1: Write paginated candidate-collection tests**

  Fake two `list=allcategories` pages queried with `acprefix=For`, `acprop=size`, continuation and a final direct categoryinfo pass for allowlisted names. Include pure guitar, electric, bass, voice, mixed, zero-page and malformed results. Assert only nonzero pure-guitar candidates reach comparison, but excluded candidates remain in `filtered_candidates` with reason evidence.

- [ ] **Step 2: Write empty/deleted/count-change tests**

  For an allowlisted name absent from allcategories, direct categoryinfo with `missing=False,size=0` means `empty_categories`; `missing=True` means `deleted_categories`. Compare live sizes with the supplied frozen run’s `CategorySnapshot.member_count` for `member_count_changes`; no hard-coded 2026 count is a pass condition.

- [ ] **Step 3: Write rename and run-association tests**

  When `compare_run_id` is supplied, fetch paginated `categorymembers` for every new pure candidate before rename comparison. A `possible_renames` item requires a missing/empty allowlisted source, a new candidate, Levenshtein distance ≤6 after normalized spacing/hyphen folding, and Jaccard overlap ≥0.80 between the frozen source member IDs and candidate live member IDs. Below either threshold stays only `new_candidates`. Report fields are `schema_version`, `generated_at`, `phase` (`standalone|run_start|run_end`), `compare_run_id`, `allowlist_version`, `allowlist_sha256`, the five result groups, and `filtered_candidates`.

  Standalone writes the latest alias `metadata/category_drift_report.json`. Run phases also write immutable `metadata/runs/<run-id>-category-drift-start.json` or `...-end.json`, update the alias only after the immutable file fsyncs, and atomically persist path/SHA-256 into mutable `metadata/runs/<run-id>-state.json` (`RunState`). The completed `RunSnapshot` remains byte-immutable; its SHA-256 is recorded in RunState. Assert start/end reports carry the same run ID, both immutable files survive, recorded digests match, snapshot bytes remain unchanged, and approved config bytes remain unchanged.

- [ ] **Step 4: Run tests and confirm failure**

  Run: `python -m pytest tests/test_discovery.py -q`

  Expected: import failure for `discovery`.

- [ ] **Step 5: Implement API candidate collection and structural filtering**

  Page the client’s allcategories/categoryinfo APIs, and candidate categorymembers when run comparison requires rename evidence. Preserve exact source names and filter with explicit reason codes. Never mutate configuration.

- [ ] **Step 6: Implement run comparison and atomic report output**

  Implement exact empty/deleted/count/rename rules above and atomically write `metadata/category_drift_report.json`. Do not expose any function that writes `config/categories.json`.

- [ ] **Step 7: Run tests and commit**

  Run: `python -m pytest tests/test_discovery.py -q`

  Expected: all discovery tests pass and the fixture allowlist remains byte-identical.

  Run: `git diff --check`

  Commit:

  ```bash
  git add scripts/imslp_library/discovery.py tests/test_discovery.py tests/fixtures/api/allcategories-guitar.json
  git commit -m "feat: report IMSLP category drift without scope expansion"
  ```

### Task 7: Implement the resumable downloader and access-state taxonomy

**Files:**
- Create: `scripts/imslp_library/downloader.py`
- Create: `tests/test_downloader.py`
- Create: `tests/fixtures/http/bot-check.html`
- Create: `tests/fixtures/http/login.html`
- Create: `tests/fixtures/http/membership-wait.html`
- Create: `tests/fixtures/http/membership-required.html`
- Create: `tests/fixtures/http/copyright.html`
- Create: `tests/fixtures/http/region.html`
- Create: `tests/fixtures/http/commercial.html`
- Create: `tests/fixtures/http/deleted.html`
- Create: `tests/fixtures/http/not-a-pdf.html`
- Create: `tests/fixtures/http/source-override.json`

- [ ] **Step 1: Add executable PDF/HTML response helpers and success tests**

  In `tests/test_downloader.py`, define `pdf_response(path)` as `bytes_response(path.read_bytes(), "application/pdf", final_url="https://s9.imslp.org/files/score.pdf")` and `target_for_path(path, file_id="301", include_sha1=True)` returning `make_download_target(score=make_score(...))` with exact size plus computed SHA-1, or `sha1_imslp=None,source_hash_missing=True`. Use `FakeTransport`, `FakeClock`, `write_minimal_pdf` and typed model factories; no `VALID_PDF`, `run_batch`, `html_fixture`, `score` or `pdf_transport` globals are allowed.

  Also define `fixture_response(relative)` by reading `tests/fixtures/<relative>` and returning `bytes_response(..., "text/html")`; `valid_pdf(tmp_path)` by `write_minimal_pdf(tmp_path / "valid.pdf")`; and `two_targets()` as two `make_download_target` values with distinct membership/category/source IDs. These are the exact helpers used by the batch-stop test below.

  Assert valid response becomes `downloaded_verified`, stores one SHA-256 object and one atomic attempt record. Size/SHA-1/pypdf mismatch goes to quarantine with `manual_review` and no active object. Missing IMSLP SHA-1 may store a structurally verified PDF with `status=downloaded_verified`, but must set `review_status=pending`, `source_hash_missing=True` and a review evidence path; completion remains blocked until a separate review record transitions it to `resolved` without claiming source-hash verification.

- [ ] **Step 2: Write exact access-state and batch-stop tests**

  Map fixture evidence to: bot check→`human_verification_required`; login→`login_required`; ordinary countdown→`membership_wait_pending`; permanent membership→`membership_required`; copyright, region, commercial and deleted fixtures→their namesake terminal status. Every HTML result saves exact bytes under `metadata/runs/<run-id>/evidence/<source-id>-<attempt>.html` and records SHA-256. Batch bot check leaves later items `not_started`; no HTML appears in `objects/`.

  ```python
  def test_bot_check_stops_batch_without_storing_html(tmp_path):
      responses = [fixture_response("http/bot-check.html"), pdf_response(valid_pdf(tmp_path))]
      results = download_batch(tmp_path, "r1", two_targets(), FakeTransport(responses), FakeClock.fixed())
      assert results[0].result.status is DownloadStatus.HUMAN_VERIFICATION_REQUIRED
      assert results[1].result.status is DownloadStatus.NOT_STARTED
      assert not list((tmp_path / "objects").rglob("*.pdf"))
  ```

- [ ] **Step 3: Write membership-wait restart tests with injected time**

  At time T, a countdown fixture yields `membership_wait_pending` with `retry_after=T+60s`. Reopen the atomic attempt manifest with a new downloader: at T+59 no request is made; at T+60 the same source is mandatory retry input. A valid response then appends `downloaded_verified`, clears the active wait and preserves both attempts. An elapsed but untried wait blocks completion.

- [ ] **Step 4: Write retry, I/O, capacity and source-policy tests**

  Timeout/connection/429/temporary 5xx use the exact configured attempt cap and end `retryable` with `detail=retry_exhausted`. Inject `ENOSPC`, `EACCES` and missing target root to assert `retryable` details `disk_full`, `permission_denied`, `target_unavailable`; inject capacity guard failure for `insufficient_capacity`. Redirect outside `imslp.org` or its subdomains becomes `manual_review/unapproved_redirect`; a different size/SHA-1 for the same source revision becomes `manual_review/source_metadata_conflict`. No case creates an active object.

  A legal override fixture targets `metadata/overrides/download_sources/<source-id>.json` and contains exact `schema_version`, `source_id`, `page_revision_id`, `url`, nonempty `legal_source_note`, `expected_size`, `sha1`, `sha256`, and timezone-aware `approved_at`. Only a revision/hash-exact fixture yields `source_override_verified`; changed source ID/revision/hash, blank note or different final URL is rejected as manual review.

- [ ] **Step 5: Write attempt-manifest and resume-selector tests**

  `metadata/runs/<run-id>-download-attempts.json` uses `atomic_write_models` envelope `schema_version=1`, `model_type="DownloadAttemptManifest"` and typed `DownloadAttempt` items. On first creation, atomically set `RunState.download_attempt_manifest_path` to that normalized root-relative path; on restart require exact path/run/envelope identity, and never repoint it to another manifest. Inject a crash after manifest creation before state update: recovery finds the one exact deterministic path, validates it, fills the pointer and continues; a conflicting file blocks. Assert atomic restart selection by `DownloadTarget.category_name`, source IDs, statuses and batch limit; already verified/terminal items are skipped, pending reviews remain visible, and incomplete `.part` paths are exact/run-scoped under `objects/.parts/<run-id>/`.

  Inject `stream_response((first_32_bytes, ConnectionError("mid-stream")))`; assert attempt bytes/part path persist. On restart the policy is restart-from-zero, not HTTP Range: move the retained part to `quarantine/download-parts/<run-id>/`, record it in `quarantine/manifests/download-parts-<run-id>.json` with items `{source_id,part_path,quarantine_path,size,sha256,reason,quarantined_at}`, create a fresh same-path part, and verify manifest equality plus final object bytes/hash equal the full response. Assert no `Range` header and no stale bytes remain.

  Source-hash reviews live at `metadata/overrides/source_hash_reviews/<source-id>.json` as typed `SourceHashReview`. Tests accept only exact source ID, page revision and object SHA-256; replay against a changed revision/hash or blank note is rejected, `reject` never resolves success, and an accepted exact review changes only `review_status` to `resolved` while preserving `source_hash_missing=True`.

  Add two DownloadTargets in different categories with the same `source_id`, revision, size and SHA-1. `download_batch` must group by source ID/revision, make exactly one transport request and one object, and return one typed `DownloadBatchResult` whose `result` is the source-level `DownloadResult` and whose stable-sorted `(category_names, membership_ids)` pairs contain both targets. The corresponding typed `DownloadAttempt` checkpoint persists those same association tuples so restart and later materialization retain both memberships. If grouped targets disagree on source metadata, make no request and return a `DownloadBatchResult` with `manual_review/source_metadata_conflict` while preserving all associations.

- [ ] **Step 6: Run tests and confirm failure**

  Run: `python -m pytest tests/test_downloader.py -q`

  Expected: import failure for `downloader`.

- [ ] **Step 7: Implement approved-source streaming and attempt checkpoints**

  Validate initial/final host, capacity and target availability, create/validate the deterministic typed attempt manifest and atomically persist its RunState pointer, then write the attempt checkpoint and stream to the exact run-scoped `.part`. Persist byte progress and I/O failures atomically. Implement the exact crash reconciliation and restart-from-zero quarantine policy; never send Range or follow an unapproved redirect.

- [ ] **Step 8: Implement PDF validation and review boundary**

  Check `%PDF-`, expected size, IMSLP SHA-1 when known, SHA-256 always and full pypdf parse before `store_verified_pdf(..., run_id)`. Quarantine mismatches. Missing source SHA-1 creates a pending review alongside structural success; implement `resolve_source_hash_review` from the exact typed review path/schema and replay guards in Step 5.

- [ ] **Step 9: Implement access/retry/wait state transitions**

  Implement exact terminal evidence, retry cap, bot-stop and injected-clock membership wait behavior. Enforce the same post-status transition sets later locked by Task 11: retry only `not_started|retryable|human_verification_required|membership_wait_pending`, or resolved-evidence `manual_review`; never retry verified success, login, copyright, region, permanent membership, commercial or deleted status. Group DownloadTargets by source ID/revision before selection/download; build one `DownloadBatchResult` per group and persist its stable-sorted category/membership pairs on every `DownloadAttempt`. Resume selectors reconstruct groups from typed targets plus those manifest associations, not filenames. Require a structured legal override with exact URL/source note/size/hashes.

- [ ] **Step 10: Run tests and commit**

  Run: `python -m pytest tests/test_downloader.py -q`

  Expected: all status and PDF validation tests pass.

  Run: `git diff --check`

  Commit:

  ```bash
  git add scripts/imslp_library/downloader.py tests/test_downloader.py tests/fixtures/http
  git commit -m "feat: add resumable verified PDF downloads"
  ```

### Task 8: Audit and stage the legacy three-guitar library

**Files:**
- Create: `scripts/imslp_library/legacy_audit.py`
- Create: `scripts/imslp_library/archive.py`
- Create: `scripts/imslp_library/staging.py`
- Create: `scripts/imslp_library/treehash.py`
- Create: `tests/test_legacy_migration.py`
- Create: `tests/fixtures/legacy/score_manifest-sample.json`
- Create: `tests/fixtures/legacy/catalog-sample.json`
- Create: `tests/fixtures/legacy/composer_translations-sample.json`
- Create: `tests/fixtures/legacy/cache/exact-arrangement-401.wiki`
- Create: `tests/fixtures/legacy/cache/bach-mixed-402.wiki`
- Create: `tests/fixtures/legacy/cache/work-level-501.wiki`

- [ ] **Step 1: Create the exact legacy manifest fixture**

  Use nine rows: one exact `For 3 Guitars` PDF, one work-level PDF, one unrelated non-PDF exclusion, and these six exact rows under section `*For 3 Guitars and Double Bass or Bass Guitar (Rest)` on `Wachet auf, ruft uns die Stimme, BWV 140 (Bach, Johann Sebastian)`:

  ```text
  PMLP149924-WACHET_AUF_BWV140_3gtrs+bass_SCORE.pdf
  PMLP149924-WACHET_AUF_BWV140_Bass_Guitar.pdf
  PMLP149924-WACHET_AUF_BWV140_Contrabass.pdf
  PMLP149924-WACHET_AUF_BWV140_Guitar_1.pdf
  PMLP149924-WACHET_AUF_BWV140_Guitar_2.pdf
  PMLP149924-WACHET_AUF_BWV140_Guitar_3.pdf
  ```

  Every row contains page title/ID/revision, file ID, filename, legacy section, relative path, expected size and SHA-1. `exact-arrangement-401.wiki` contains the exact `For 3 Guitars` branch and file; `bach-mixed-402.wiki` contains the exact mixed heading and all six file templates; `work-level-501.wiki` contains the minimal work-level Scores/Parts block with `Instrumentation=3 guitars`. Manifest rows point to these exact revisions; a work-level mutation uses `3 guitars and double bass`. Audit tests delete legacy `section` values before re-extraction to prove cached wikitext, not old labels, determines purity.

- [ ] **Step 2: Write read-only audit and strict re-extraction tests**

  Build the temporary legacy tree with generated valid PDFs and matching fixture sizes/hashes. Assert audit does not change any source stat/hash; ignores old section trust and reruns the new extractor against exact cached revision; sends all six known mixed rows to exclusion reason `mixed_instrument_heading`; leaves the work-level row `manual_review` until exact evidence is applied; and computes counts from rows. Quarantine manifest schema is `schema_version`, `run_id`, `reason_scope="legacy_mixed"`, and `items[{source_path,backup_path,quarantine_path,reason,size,sha256}]`; none of the six enters active object/membership output.

- [ ] **Step 3: Write archive preflight and strategy tests**

  Define injectable `ArchiveEnvironment(volume_name(root), disk_usage(root), run_clone(source,destination), copy_file(source,destination), chmod(path,mode), fsync_dir(path))`; production uses mounted-volume metadata, `shutil.disk_usage`, `/bin/cp -cR`, `shutil.copy2`, `os.chmod` and directory fsync, while tests use `FakeArchiveEnvironment` over `tmp_path`. Let `base_bytes = legacy_apparent_bytes + staging_headroom`, `safety_margin = max(ceil(base_bytes * 0.20), 1 GiB)`, and `required_free = base_bytes + safety_margin`. Preflight requires `volume_name(root)=="PHILIPS"`, rejects existing final/partial/retired/staging collision, unsupported links, or free space below `required_free`. Inject clone failure and assert fallback copy only when worst-case full-copy `required_free` passes; otherwise no backup/staging path is created.

- [ ] **Step 4: Write partial backup resume and read-only manifest tests**

  Backup first targets `backups/.for3guitars-pre-migration-<run-id>.partial`; progress lives outside it at `backups/manifests/.for3guitars-pre-migration-<run-id>.progress.json`, so copied-tree equality is unaffected. Inject copy failure after two files, rerun, skip exact matches and complete missing files. After full equality, atomically rename to `backups/for3guitars-pre-migration-<run-id>/`, atomically replace progress with canonical external `backups/manifests/for3guitars-pre-migration-<run-id>.json`, chmod files `0444` and directories `0555`, and reverify. No final backup path exists before equality.

- [ ] **Step 5: Write idempotent staging tests without Chunk 3 dependencies**

  Build `_migration/<run-id>/For 3 guitars (arr)/metadata/` with eligible works/files/memberships/path map and category links only. Import eligible PDFs via object store and materializer; rerun is byte-identical. For every imported eligible source, persist a typed finished `DownloadAttempt` at `metadata/runs/<run-id>-download-attempts.json` with `attempt_number=1`, `status=downloaded_verified`, `detail_code=legacy_integrity_import`, exact category/membership associations, bytes written and audit-evidence path/hash; this is migration provenance, not a network claim. Assert old source hashes unchanged, mixed files absent and every stage link identifies the recorded object. Write `metadata/staging-assembly-report.json` with exact fields `schema_version`, `run_id`, absolute `staging_path`, absolute `virtual_final_path`, `included_paths:["metadata/works.json","metadata/score_files.json","metadata/memberships.json","metadata/path_map.json","metadata/runs/<run-id>-download-attempts.json","scores"]`, `membership_manifest_sha256`, `score_file_manifest_sha256`, `path_map_sha256`, `download_attempt_manifest_sha256`, `assembly_tree_sha256`, `complete:true`, `assembled_at`. `assembly_tree_sha256` is the immutable migration source identity and covers only those exact paths so later translation/render outputs do not invalidate the core assembly record. Do not seed translations, render catalogs or call final verifier here; those are integrated after Chunk 3 exists.

  `treehash.logical_tree_sha256(tree, library_root, virtual_final_path, included_paths=None, excluded_paths=())` walks NFC root-relative POSIX paths sorted by UTF-8 bytes; includes `D\0<path>\n` for directories, `F\0<path>\0<size>\0<content-sha256>\n` for regular files/hardlinks, and `L\0<path>\0<link-text>\0<target-content-sha256>\n` for relative symlinks. It rejects special files, absolute links, targets outside `library_root`, links not ending under `library_root/objects/<prefix>/<sha256>.pdf`, and normalization collisions. For staging symlinks, resolve link text from the future `virtual_final_path`; for active trees resolve from the actual final path. Thus a legal category link may leave the category tree but never the library root/object namespace. Assembly uses the exact include list above. Final staging/activation hashes include the whole tree except exact `metadata/staging-assembly-report.json`, `metadata/verification/*.json`, `metadata/render-manifest.json`, `metadata/render-manifests/*.json`, and sibling `*.tmp`; those report digests are validated separately. The digest is SHA-256 of the concatenated records, so report bytes never recursively hash themselves. Tests cover hardlinks, valid category→object relative links, an escaping link, NFC collision and equal pre/post-activation digests.

- [ ] **Step 6: Run tests and confirm failure**

  Run: `python -m pytest tests/test_legacy_migration.py -q`

  Expected: import failures for `legacy_audit`, `archive` and `staging`.

- [ ] **Step 7: Implement read-only legacy audit**

  Emit `metadata/migrations/<run-id>-legacy-audit.json` with old total, eligible, mixed-excluded, work-level-review and integrity-error counts plus per-row evidence. The optional override is exactly `metadata/overrides/work_level_review_<run-id>.json` with `schema_version:1`, `run_id`, `source_audit_sha256`, and unique `review_id`-sorted `items[{review_id,page_id,revision_id,source_id,file_id,filename,legacy_relative_path,instrumentation_raw,instrumentation_normalized,instrumentation_sha256,decision,reason,reviewed_at}]`. Normalize `legacy_relative_path` to NFC POSIX text with no leading slash, `.` or `..`; then `review_id=sha256(json.dumps([page_id,revision_id,source_id,file_id,legacy_relative_path],ensure_ascii=False,separators=(",", ":")).encode("utf-8")).hexdigest()`. Decision is `work_level_exact_instrumentation|exclude`, reason is nonblank and time is aware. Apply each row only when run ID, audit digest, every file/page identity and instrumentation hash match; this permits multiple reviewed files on one page while rejecting duplicate/stale/partial/extra rows.

- [ ] **Step 8: Implement preflight and resumable verified archive**

  Implement the exact clone/fallback/progress/final-manifest/read-only behavior from Steps 3–4. Dry run may atomically write audit/preflight reports but creates no backup, object, quarantine file, staging or renamed directory.

- [ ] **Step 9: Implement eligible staging and mixed quarantine records**

  Stage only resolved eligible data, materialize links and write the exact assembly report. Relative-symlink staging writes the final-location-relative link text and validates it through `virtual_final_path`; activation renames without rewriting links. Create `quarantine/legacy-mixed-<run-id>/files/` as hardlinks to the verified backup when supported, otherwise relative symlinks; write its independent manifest. It remains outside active object/dedupe invariants.

- [ ] **Step 10: Run tests and commit**

  Run: `python -m pytest tests/test_legacy_migration.py -q`

  Expected: all legacy audit, backup and staging tests pass; fixture old tree hashes are unchanged.

  Run: `git diff --check`

  Commit:

  ```bash
  git add scripts/imslp_library/legacy_audit.py scripts/imslp_library/archive.py scripts/imslp_library/staging.py scripts/imslp_library/treehash.py tests/test_legacy_migration.py tests/fixtures/legacy
  git commit -m "feat: stage strict migration of the legacy guitar library"
  ```

### Task 9: Add journaled cutover and crash recovery

**Files:**
- Create: `scripts/imslp_library/migration.py`
- Create: `tests/test_migration_recovery.py`

- [ ] **Step 1: Lock the complete durable journal schema**

  Journal fields are `schema_version`, `run_id`, `state`, exact absolute `old_path`, `retired_path`, `staging_path`, `final_path`, `failed_activation_path`, `legacy_tree_sha256`, `backup_manifest_sha256`, `staging_report_path`, `staging_report_sha256`, `staging_tree_sha256`, `post_verification_report_path`, `post_verification_report_sha256`, `intended_action:{name,source,destination,prepared_at}|null`, `completed_actions:[{name,source,destination,completed_at}]`, `last_error`, `created_at`, `updated_at`. `staging_report_path` is the immutable canonical complete scope report, `staging_report_sha256` is its canonical self-hash, and `staging_tree_sha256` is read only from validated `report.facts["staging_tree_sha256"]` and must be 64 lowercase hex; the activation confirmation value is the report self-hash. The deterministic post-verification record is exactly `metadata/verification/<run-id>-migration-post.json` with fields `schema_version`, `run_id`, exact absolute `final_path`, `final_tree_sha256`, `verification_report_path`, `verification_report_sha256`, `complete:true`, and timezone-aware `verified_at`; its referenced verification path is also immutable canonical, never a latest alias. Valid states are `planned`, `backup_verified`, `staging_verified`, `legacy_moved`, `category_activated`, `post_verified`, `rollback_required`, `rolled_back`. Every update uses atomic JSON + file/parent fsync.

- [ ] **Step 2: Write the exact normal/recovery path matrix**

  Assert these only-valid existence tuples `(old,retired,staging,final,failed_activation)` and deterministic outcomes:

  | Journal/action state | Tuple | Recovery |
  |---|---|---|
  | `planned`, no intent | `1,0,0,0,0` | no-op; wait for verified backup |
  | `backup_verified`, no intent | `1,0,0,0,0` | no-op; wait for verified staging |
  | `staging_verified`, no intent | `1,0,1,0,0` | no-op; only explicit activate starts rename |
  | `staging_verified`, move-legacy intent, rename not done | `1,0,1,0,0` | perform move |
  | same intent, rename happened before journal completion | `0,1,1,0,0` | fsync/record `legacy_moved` |
  | `legacy_moved`, activate intent, rename not done | `0,1,1,0,0` | perform activation |
  | same intent, activation happened before journal completion | `0,1,0,1,0` | fsync/record `category_activated` |
  | `category_activated` | `0,1,0,1,0` | run post-verifier once; persist digest or enter rollback |
  | `post_verified` | `0,1,0,1,0` | no-op |
  | `rollback_required`, preserve-final pending | `0,1,0,1,0` | move final to failed path |
  | `rollback_required`, final already preserved | `0,1,0,0,1` | fsync/record preservation, restore retired |
  | `rollback_required`, retired already restored | `1,0,0,0,1` | fsync/record restore, mark `rolled_back` |
  | `rolled_back` | `1,0,0,0,1` | no-op |

  Any other tuple blocks with `unexpected_path_state`; no globbing or overwrite is allowed.

- [ ] **Step 3: Write failure-injection tests at every rename boundary**

  Inject via `fault_hook(event_name)` at `before_legacy_rename`, `after_legacy_rename_before_fsync`, `after_legacy_fsync_before_journal`, `before_activation_rename`, `after_activation_rename_before_fsync`, `after_activation_fsync_before_journal`, and `after_post_verify_before_journal`. Restart and assert the matrix outcome, exact digest identities, both parent-directory fsync calls and no state where old and final are both missing without retired or failed-activation recovery data. In the last case the deterministic report already exists; recovery accepts it only when `run_id`/absolute `final_path` match, `complete is true`, a fresh final-tree hash equals `final_tree_sha256`, and the referenced verification report exists with matching SHA-256. Assert the verifier callback call count remains exactly one; any missing, malformed or mismatched record is never reused and enters the ordinary idempotent re-verification-or-rollback path.

- [ ] **Step 4: Write post-verification failure rollback tests**

  Inject a verifier failure after final activation. Assert final is first atomically moved to `quarantine/failed-activation-<run-id>/For 3 guitars (arr)/`, independently manifested by path/size/SHA-256, then retired is atomically restored to old, journal reaches `rolled_back`, staging is absent, failed output and pre-migration backup remain recoverable. Inject at `before_failed_preserve_rename`, `after_failed_preserve_rename_before_fsync`, `after_failed_preserve_fsync_before_journal`, `before_retired_restore_rename`, `after_retired_restore_rename_before_fsync`, and `after_retired_restore_fsync_before_journal`; restart must follow the rollback rows above. If failed-activation destination or old path is occupied, journal stays `rollback_required` and no overwrite occurs.

- [ ] **Step 5: Run tests and confirm failure**

  Run: `python -m pytest tests/test_migration_recovery.py -q`

  Expected: missing cutover/recovery functions.

- [ ] **Step 6: Implement journal validation and intended/completed actions**

  Validate paths remain under the exact library root, digests match preflight/staging reports, transitions are legal, and timestamps are aware. Persist intent before rename; after rename fsync both parents, append completed action, clear intent and advance state.

- [ ] **Step 7: Implement deterministic reconciliation and activation**

  Implement the exact matrix. Require verified backup, a complete staging verification report whose self-hash matches `staging_report_sha256`, and a freshly recomputed staging logical-tree hash matching `staging_tree_sha256`; also require absent destinations and matching legacy identity. After activation, the same logical algorithm must yield the same tree identity before post-verification. Accept an idempotent `post_verify(final_path, deterministic_report_path)->report_sha256` callback so Chunk 2 has no dependency on the later verifier module; always pass `metadata/verification/<run-id>-migration-post.json`. The callback atomically writes the exact record from Step 1 only after the final-tree hash and referenced verification-report digest pass. After it returns, inject the crash hook, then persist that deterministic path/digest before `post_verified`. Recovery may skip the callback only after freshly validating every reuse condition from Step 3; otherwise it invokes the idempotent verifier, and verifier failure follows the rollback state machine.

- [ ] **Step 8: Implement safe rollback after activation failure**

  Preserve failed final output first, manifest it, restore retired only when old is absent and identity matches, and persist every step. Never delete backup, failed output or unexpected directories.

- [ ] **Step 9: Run Chunk 2 verification and commit**

  Run:

  ```bash
  python -m pytest tests/test_client.py tests/test_snapshot.py tests/test_discovery.py tests/test_downloader.py tests/test_legacy_migration.py tests/test_migration_recovery.py -q
  git diff --check
  ```

  Expected: all Chunk 2 tests pass, including every exact-revision/cache, access-state/wait, archive/staging and rename/post-verify failure regression.

  Commit:

  ```bash
  git add scripts/imslp_library/migration.py tests/test_migration_recovery.py
  git commit -m "feat: recover safely from migration cutover crashes"
  ```

### Chunk 2 traceability gate

| Specification requirement | Required test evidence before Chunk 2 closes |
|---|---|
| Design §§3.4/6.2 frozen scope | Initial incomplete checkpoint, per-category/batch resume, exact revision-by-ID, config hash guard, complete-only transition |
| Design §3.4 discovery/drift | paginated API collection, filtered evidence, empty vs deleted, count changes, rename thresholds, start/end run association, config bytes unchanged |
| Design §6.5 downloads | approved hosts/redirects, PDF header/size/SHA-1-if-known/SHA-256/pypdf, attempt envelope, all access states, terminal evidence, retry exhaustion, bot batch stop |
| Design §§6.5/9 review and waits | missing-SHA review pending/resolved boundary, injected-clock wait persistence, no early retry, mandatory elapsed retry, completion blocking |
| Design §7 legacy purity | exact six mixed filenames excluded, cached exact-revision re-extraction, work-level review evidence, no old-tree mutation |
| Design §§7/10.2 archives | clone/copy preflight, partial resume, independent equality manifest, read-only backup, independent mixed quarantine manifest |
| Design §7 staging | eligible-only object import, category paths, structural report, idempotent rerun, no dependency on later render/verify modules |
| Design §§7/10.3 cutover | complete journal schema, every rename crash boundary, four-path matrix, parent fsync, post-verify failure preservation and rollback |

## Chunk 3: Translations, offline catalogs, verification, and CLI

### Task 10: Add shared composer and title translations

**Files:**
- Create: `metadata/translations/composers_zh.json`
- Create: `metadata/translations/title_overrides_zh.json`
- Create: `metadata/translations/title_terms_zh.json`
- Create: `scripts/imslp_library/translations.py`
- Create: `tests/test_translations.py`
- Modify: `scripts/imslp_library/enums.py`
- Modify: `scripts/imslp_library/models.py`
- Modify: `tests/model_helpers.py`

- [ ] **Step 1: Lock translation types and the three exact JSON schemas**

  Add `TranslationSourceType={legacy_curated,manual_common_name,manual_transliteration,rule_generated,fallback}` and frozen `Translation(entity_kind:str, english:str, chinese:str, source_type:TranslationSourceType, source_ref:str, fallback:bool)`. `entity_kind` is exactly `composer|title`; English and Chinese are nonempty NFC strings; `fallback` is true if and only if `source_type=fallback`. Translation functions return raw Unicode data and never HTML/CSV/URL escaping.

  `composers_zh.json` is exactly:

  ```json
  {
    "schema_version": 1,
    "data_version": "2026-08-30.1",
    "legacy_source": {
      "path": "for3guitars/metadata/composer_translations_zh.json",
      "sha256": "784a1942a957bb0b159528d994c4ee87f9e66a6a28b4bb79c5bf2f473fa27724",
      "entry_count": 199
    },
    "items": [
      {"english": "Agazzari, Agostino", "chinese": "阿戈斯蒂诺·阿加扎里", "source_type": "legacy_curated", "source_ref": "legacy:composer_translations_zh.json"}
    ]
  }
  ```

  `title_overrides_zh.json` has the same top-level keys and item fields, with legacy path `for3guitars/metadata/translations_zh.json`, SHA-256 `f5040c2e5820a3c8d3c46955368568999682677cdb8fd266af8ab2eda27f6700`, entry count `208`, and `source_ref="legacy:translations_zh.json"`. Both item arrays sort by `(english.casefold(), english)`, reject duplicate exact or case-folded English keys, reject unknown source types, and preserve legacy Chinese values byte-for-byte after NFC normalization. Because the old files do not encode whether a name is common usage or transliteration, every imported row is truthfully labeled `legacy_curated`; only newly reviewed entries may use `manual_common_name` or `manual_transliteration`.

  `title_terms_zh.json` is exactly `schema_version`, `data_version`, and `rules[{rule_id,priority,pattern,replacement_zh,source_ref}]`. Rules sort by `(priority,rule_id)`, priorities are positive integers, patterns must be full anchored (`^...$`) regexes, replacements may reference only named groups declared by their pattern, and duplicate rule IDs or same-priority multi-match for one title are rejected. Seed only reviewed form/key/number/opus rules required by fixtures; never use a partial token substitution as a completed title translation.

- [ ] **Step 2: Write failing model, import, precedence and fallback tests**

  Assert `Translation` validation/round-trip, exact legacy path/hash/count checks, deterministic conversion of all 199 composer and 208 title entries, case-fold duplicate rejection, and unchanged English keys. Assert exact title override outranks rule matching; a single structured rule deterministically formats form/key/opus groups; ambiguous rules fail closed. Unknown composer returns raw `暂无可靠中译（原名：<original>）` with `fallback=True`; unknown title returns raw `暂无通行中译（原题：<original>）`. Put `<script>`, quotes, commas and Chinese in fallback inputs and assert translations retain raw characters—the renderer, not this layer, escapes them. Assert every legacy composer maps to a nonempty Chinese value and no imported title is silently dropped.

- [ ] **Step 3: Run tests and confirm failure**

  Run: `python -m pytest tests/test_translations.py -q`

  Expected: missing enum/model/module and translation files.

- [ ] **Step 4: Import exact legacy seeds and implement translation precedence**

  Verify both legacy source hashes before conversion, write the exact deterministic schemas, then implement `load_translation_catalogs`, `translate_composer` and `translate_title`. Precedence is exact manual/legacy override → exactly one structured rule → explicit fallback. Cache indexes by exact English key plus a duplicate-detection case-fold index; never silently case-fold a lookup into a different spelling.

- [ ] **Step 5: Run tests, check whitespace and commit**

  Run: `python -m pytest tests/test_translations.py -q`

  Expected: all translation tests pass with exactly 199 imported composer rows and 208 imported title rows.

  Run: `git diff --check`

  Commit:

  ```bash
  git add metadata/translations scripts/imslp_library/enums.py scripts/imslp_library/models.py scripts/imslp_library/translations.py tests/model_helpers.py tests/test_translations.py
  git commit -m "feat: add shared bilingual guitar catalog names"
  ```

### Task 11: Render self-contained root and category catalogs

**Files:**
- Create: `scripts/imslp_library/selection.py`
- Create: `scripts/imslp_library/render.py`
- Create: `scripts/imslp_library/assets/catalog.js`
- Create: `tests/test_render.py`
- Create: `tests/render_helpers.py`
- Create: `tests/fixtures/render/library_dataset.json`
- Create: `tests/fixtures/render/search_cases.json`
- Modify: `scripts/imslp_library/models.py`
- Modify: `tests/model_helpers.py`

- [ ] **Step 1: Lock the exact typed render dataset**

  Add frozen models with tuples for collections:

  | Model | Exact fields |
  |---|---|
  | `CatalogCategory` | `name:str`, `kind:CategoryKind`, `display_group:str`, `guitar_counts:tuple[int,...]`, `extended_strings:bool` |
  | `CatalogRecord` | `category_name:str`, `membership_id:str`, `work_id:str`, `source_id:str`, `title_en:str`, `title_zh:str`, `title_zh_source:TranslationSourceType`, `composer_en:str`, `composer_zh:str`, `composer_zh_source:TranslationSourceType`, `imslp_url:str`, `filename:str`, `local_path:str|None`, `download_status:DownloadStatus`, `selection_reason:SelectionReason` |
  | `CategorySelection` | `schema_version:int`, `run_id:str`, `mode:str` (`category|categories_file|all_approved`), `category_names:tuple[str,...]`, `source_path:str|None`, `selection_sha256:str` |
  | `LibraryDataset` | `schema_version:int`, `run_id:str`, `source_kind:str` (`run_snapshot|legacy_assembly`), `source_manifest_sha256:str`, `selection:CategorySelection`, `generated_at:datetime`, `categories:tuple[CatalogCategory,...]`, `records:tuple[CatalogRecord,...]` |
  | `RenderSummary` | `schema_version:int`, `run_id:str`, `selection_sha256:str`, `layout:str` (`library_root|category_root`), `manifest_path:str`, `manifest_sha256:str`, `root_outputs:tuple[str,...]`, `category_outputs:tuple[tuple[str,tuple[str,...]],...]`, `category_count:int`, `work_count:int`, `source_count:int`, `membership_count:int` |

  Validate stable IDs, known category references, unique membership IDs, one consistent work/source identity, aware time and 64-hex hashes. `guitar_counts` is unique/sorted/strictly positive; ordinary fixed-count categories contain one value, `For 2 and 3 guitars (arr)` contains `(2,3)`, and ensemble/orchestra categories use `()`. A verified-success record requires a normalized library-root-relative POSIX `local_path`; terminal-unavailable records require `local_path=None` and never generate an href. Normal datasets use `source_kind=run_snapshot` and the immutable RunSnapshot SHA-256; migrated staging uses `source_kind=legacy_assembly` and `staging-assembly-report.json` field `assembly_tree_sha256`. Dataset builders sort categories by name and records by `(composer_en.casefold(),title_en.casefold(),category_name,membership_id)`.

  A categories file has exact schema `schema_version:1`, `run_id`, `categories` as a unique sorted nonempty approved-name array, and `selection_sha256`; the digest is SHA-256 of canonical JSON for the first three fields. The input file must resolve inside the library root; `CategorySelection.source_path` is its normalized library-root-relative POSIX path with no `..` (for example `metadata/runs/<run-id>-pilot-categories.json`), while direct-category/all-approved selections require `source_path=None`. `build_category_selection` validates path containment, run binding/config membership and creates the same canonical digest for direct-category and all-approved selections. This one contract is shared by downloader, renderer, verifier and pilot rollout.

  To derive `CatalogRecord.download_status`, load the typed attempt manifest named by RunState for normal runs (an absent pointer means no attempts yet) or the assembly report for migration. Per source, attempt numbers are unique and strictly increasing. No later attempt is legal after `{downloaded_verified,source_override_verified,login_required,copyright_restricted,region_restricted,membership_required,commercial_only,deleted}`. Later attempts are legal after `{not_started,retryable,human_verification_required,membership_wait_pending}` subject to downloader timing/user-resume rules. A later attempt after `manual_review` additionally requires the prior attempt to have `review_status=resolved` and an existing hash-matching resolution/override evidence path; otherwise history is invalid. For each membership, choose the single highest `(attempt_number,started_at)` associated attempt first, then expose its current phase/status: started/streaming becomes `not_started`, finished uses stored status, and no attempt becomes `not_started`. Do not fall back to an older finished attempt when a higher retry is active. A grouped attempt supplies the same source status to every persisted membership association. `Membership.local_path` is present if and only if effective status is `downloaded_verified|source_override_verified`; every other status requires it absent.

- [ ] **Step 2: Lock fixture builders and every output contract**

  `tests/fixtures/render/library_dataset.json` is a tagged `LibraryDataset` containing duplicate work memberships, one terminal-unavailable record, `For 2 and 3 guitars (arr)`, plus paths with spaces, `#`, `%`, apostrophe, Chinese, NFC/NFD variants, long components, cleaning collisions and a title containing `</script>`. `tests/render_helpers.py` implements `load_render_fixture`, `local_hrefs(page)` with `html.parser.HTMLParser`, `markdown_hrefs(page)` for inline Markdown links, `resolve_local_href(page,href)` using `urllib.parse.urlsplit/unquote` per path component, `inline_dataset(page)`, and `run_filter_js(records,cases)` by executing the exact asset with `node` in a temporary harness. Run `node --version` before tests and fail with an environment diagnostic if unavailable.

  Layout `library_root` writes output-root-relative owned paths `index.html`, `catalog.csv`, `score_manifest.csv` and `metadata/catalog.json`; a selected category also writes `<category>/index.html`, `README.md`, `乐谱库目录.md`, `catalog.csv`, `score_manifest.csv` and `metadata/catalog.json`. Layout `category_root` requires exactly one category, writes only that category’s `index.html`, `README.md`, `乐谱库目录.md`, `catalog.csv`, `score_manifest.csv` and `metadata/catalog.json` directly at the output root, and strips the exact `<category>/` prefix only when computing hrefs. It never creates a nested `<category>/` directory or a global root catalog. For `category_root`, `RenderSummary.root_outputs=()` and `category_outputs=((category_name,("index.html","README.md","乐谱库目录.md","catalog.csv","score_manifest.csv","metadata/catalog.json")),)`. Owned strings never start with `/`; HTML/Markdown hrefs are page-relative, component-encoded paths distinct from manifest paths.

  Each JSON document has exact fields `schema_version`, `run_id`, `scope` (`root|category`), `category_name`, `generated_at`, `summary{category_count,work_count,source_count,membership_count,available_membership_count}`, `catalog_rows`, and `score_rows`. `catalog_rows` are unique `(category_name,work_id)` rows with exact fields `category_name,work_id,title_en,title_zh,title_zh_source,composer_en,composer_zh,composer_zh_source,imslp_url,score_count`; `score_rows` are one per membership with exact fields from `CatalogRecord` plus nullable page-relative `href`. `href` is present only when `local_path` exists and is empty in CSV otherwise. `catalog.csv` uses those catalog-row fields in that order; `score_manifest.csv` uses score-row fields in model order followed by `href`; UTF-8 CSV uses a header, RFC-4180 quoting and `\n` line endings. Markdown and HTML derive from the same rows and summary.

  The canonical manifest path is output-root-relative `metadata/render-manifests/<run-id>-<selection-sha12>.json`; `metadata/render-manifest.json` is an atomic latest alias with identical bytes. Exact fields are `schema_version`, `run_id`, `selection_sha256`, `layout`, `generated_at`, `output_root:"."`, `owned_files[{path,size,sha256}]`, `complete:true`, `manifest_sha256`. Owned paths are unique sorted output-root-relative POSIX paths; they exclude both manifest files. `manifest_sha256` hashes canonical fields except itself. `RenderSummary` records canonical path/hash. Cleanup reads only the canonical manifest for the same `(selection_sha256,layout)`, never the latest alias; changing selection A→B cannot delete A-owned outputs, though later same-selection A rerender may clean stale A-owned files after hash/path containment checks.

- [ ] **Step 3: Write failing renderer, link and JavaScript tests**

  First build real datasets from temporary typed manifests rather than only deserializing the ready-made fixture. Cover: no RunState attempt pointer→`not_started`; atomic pointer to grouped attempts→both memberships share status; higher in-progress retry masks older finished status; finished success materializes a path; every non-success rejects a path; illegal post-terminal transition rejects; and legacy assembly digest plus `legacy_integrity_import` attempts produce verified records without a RunSnapshot. Assert the builder validates source-kind/digest, selection/run binding and attempt associations before rendering.

  Render the fixture and assert root plus every category’s JSON/CSV/Markdown/HTML counts and stable IDs agree; composers sort deterministically; English/Chinese/source labels and IMSLP attribution are present; special paths round-trip by component; every local href from every HTML and Markdown page resolves; terminal-unavailable rows have status text but no href; and HTML contains no CDN, external stylesheet/script, network URL in executable tags, local `fetch(`, absolute filesystem path or `file://` literal.

  ```python
  def test_file_urls_resolve_without_http_server(tmp_path):
      render_fixture(tmp_path)
      pages = [tmp_path / "index.html", *tmp_path.glob("For */index.html")]
      for page in pages:
          for href in local_hrefs(page):
              assert resolve_local_href(page, href).exists()
      for page in tmp_path.glob("For */*.md"):
          for href in markdown_hrefs(page):
              assert resolve_local_href(page, href).exists()
  ```

  Assert the inline dataset escapes `<`, `>`, `&`, U+2028 and U+2029 so `</script>` cannot terminate the block, yet JSON parsing restores the original. `search_cases.json` covers English/Chinese composer/title, original IMSLP strings, guitar count including membership in `(2,3)`, original/arrangement, extended strings and ensemble; execute the pure `filterCatalog(records,query,filters)` with Node and assert exact membership-ID results. Assert canonical/latest render manifests match and validate their self-hash. Test same-selection stale cleanup and A→B→A: B never deletes A-only outputs, while the final A run cleans only stale paths from A’s prior canonical manifest. Render one-category `category_root` into a staging directory and assert there is no nested category directory and every PDF href maps to the virtual final category path.

- [ ] **Step 4: Run tests and confirm failure**

  Run:

  ```bash
  node --version
  python -m pytest tests/test_render.py -q
  ```

  Expected: Node is available, then tests fail for missing render models/module/assets/helpers.

- [ ] **Step 5: Implement deterministic multi-format rendering and owned-output cleanup**

  Implement `build_category_selection` in `selection.py`, then `build_library_dataset` and `render_library(output_root,dataset,layout="library_root")` in `render.py`. Normal runs use library-root layout. Migration copies configuration/translations/manifests into the staging category, builds from that metadata root, and renders `category_root` directly into the same staging output root; object verification remains separately rooted at the global library. Generate exact contracts atomically. Write canonical/latest render manifests only after all outputs succeed; cleanup consults only a validated same-selection/layout canonical manifest and remains inside output root. Inline escaped JSON, asset CSS if any, and exact JavaScript in every HTML page.

- [ ] **Step 6: Implement and execute root search/filter behavior**

  Keep `filterCatalog` pure and DOM-independent; the small DOM adapter reads the inline dataset and data attributes, then shows matching rows. Search composer/title English and Chinese plus original IMSLP strings; filter by guitar count, original/arrangement, extended strings and ensemble. Show duplicate category tags while linking every membership to its category-local score path. Do not infer availability from link text; use typed download status.

- [ ] **Step 7: Run tests, check whitespace and commit**

  Run:

  ```bash
  node --version
  python -m pytest tests/test_render.py -q
  ```

  Expected: all rendering, all-page link resolution, inline-data safety, stale-output and executable JavaScript cases pass.

  Run: `git diff --check`

  Commit:

  ```bash
  git add scripts/imslp_library/models.py scripts/imslp_library/selection.py scripts/imslp_library/render.py scripts/imslp_library/assets/catalog.js tests/model_helpers.py tests/render_helpers.py tests/test_render.py tests/fixtures/render
  git commit -m "feat: render offline bilingual guitar catalogs"
  ```

### Task 12: Implement cross-layer verification and completion gates

**Files:**
- Create: `scripts/imslp_library/verify.py`
- Create: `tests/test_verify.py`
- Create: `tests/library_tree_helpers.py`
- Create: `tests/fixtures/verification/complete.json`
- Create: `tests/fixtures/verification/incomplete.json`
- Modify: `scripts/imslp_library/enums.py`
- Modify: `scripts/imslp_library/models.py`
- Modify: `tests/model_helpers.py`

- [ ] **Step 1: Lock scope/request models, report paths and canonical digest**

  Add `VerificationScope={staging,category,categories_file,all_approved}`, frozen `VerificationRequest(scope:VerificationScope,selection:CategorySelection,metadata_root:str,object_root:str,output_root:str)`, and frozen `VerificationArtifact(report:VerificationReport,canonical_path:str,latest_alias_path:str)`. Artifact paths are normalized library-root-relative POSIX paths; the canonical basename must equal `<report.report_sha256>.json` and the alias must match scope key. Request paths are exact absolute paths validated under the library root. Require: staging has metadata/output root `_migration/<run-id>/For 3 guitars (arr)`, object root equal to the library root, and a direct-category selection for `For 3 guitars (arr)`; category/categories-file/all-approved use the active library root for all three roots with their respective exact selectors. Migration post-verification is an adapter over category scope, not a fifth public scope.

  All scope reports are written under the stable public library root passed to `verify_library`, never under renameable `metadata_root` or `output_root`. `VerificationReport.scope/scope_key` record the request. Common `facts` keys are exact: `selection_sha256`, `source_manifest_sha256`, `score_file_manifest_sha256`, `membership_manifest_sha256`, nullable `attempt_manifest_sha256`, `render_manifest_sha256`, `checked_category_count`, `checked_work_count`, `checked_source_count`, `checked_membership_count`. Staging adds `assembly_report_sha256`, `staging_tree_sha256`, `backup_manifest_sha256`, `quarantine_manifest_sha256`; all-approved adds `runstate_sha256`, `snapshot_sha256`, `start_drift_report_sha256`, `end_drift_report_sha256`. Every digest is 64 lowercase hex and every count is nonnegative. Compute `report_sha256` as SHA-256 of canonical UTF-8 JSON for every report field except `report_sha256` and reject a loaded report whose recomputed digest differs.

  Scope keys are exactly `staging`, `category-<category-sha10>`, `set-<selection-sha12>`, and `all`. Every generated report, whether `complete=True` or `False`, is immutable at `metadata/verification/reports/<run-id>/<scope-key>/<report_sha256>.json`, created without overwriting and revalidated if already present; therefore every returned `VerificationArtifact` has both required paths. Stable latest aliases are staging `metadata/verification/<run-id>-staging.json`, category `metadata/verification/<run-id>-category-<category-sha10>.json`, category set `metadata/verification/<run-id>-set-<selection-sha12>.json`, and all-approved `metadata/verification/<run-id>.json`; aliases may atomically change and are never referenced by a migration journal/wrapper.

  Never reuse from manifest/facts equality alone. Every invocation reruns all scope checks against current object bytes, membership link targets/content, render owned-file bytes, terminal evidence bytes, archive/quarantine trees and other selected entities. After fresh checks recompute `facts`, `issues`, `complete`, scope and run ID. Only if that fresh semantic result exactly equals a valid prior canonical/latest report excluding its `verified_at` and `report_sha256` may the service return the old immutable bytes without changing time; otherwise it creates a new canonical report and updates the alias. Tests mutate each entity domain while leaving its manifest unchanged and require a new incomplete report. The journal stores only a complete staging canonical absolute path/self-hash; migration-post references only a complete immutable category canonical path/self-hash, so later verification cannot invalidate either reference.

- [ ] **Step 2: Lock exact issue codes, severity and scope gates**

  Tests require these error codes (all severity `error` and completion-blocking): `snapshot_category_mismatch`, `unapproved_category`, `extraction_evidence_missing`, `object_manifest_mismatch`, `membership_path_mismatch`, `link_target_mismatch`, `pdf_invalid`, `transient_artifact_present`, `independent_manifest_mismatch`, `manual_review_pending`, `source_hash_review_pending`, `membership_wait_pending`, `membership_wait_elapsed_unretried`, `terminal_evidence_invalid`, `translation_missing`, `local_link_broken`, `runstate_digest_mismatch`, `run_start_drift_missing`, `run_end_drift_missing`, `mixed_instrumentation`, `render_manifest_mismatch`. Use `post_snapshot_drift` as warning only and `terminal_unavailable` as info only when its evidence path/hash, HTTP status if present, and aware timestamp validate.

  Every scope checks approved-category boundary, extraction evidence for included memberships, referenced-object/link/PDF/path equality, purity, translation records needed by the scope, the selection-specific render manifest/local links and independent manifests reachable from the scope. The authority is exact typed envelope `metadata/score_files.json` with `model_type="ScoreFileManifest"`: selected scopes project selected records with non-null `sha256/object_path`, deduplicate by SHA-256, and require those exact object files; only all-approved additionally requires set equality against every regular file under `objects/<prefix>/<sha256>.pdf`, so unrelated verified objects do not break a pilot/staging check. Staging validates `metadata/staging-assembly-report.json` by recomputing its exact `included_paths` assembly identity, then separately computes the final staging logical-tree hash with the exact report exclusions, validates backup/quarantine manifests and zero unresolved legacy reviews, and persists that digest only in `report.facts["staging_tree_sha256"]`. The workflow stores canonical report path, report self-hash and that exact facts value in the journal before `staging_verified`. Staging does not require run drift. Category checks the one active category and migration evidence when present. Category-set checks exactly the canonical selection. All-approved additionally requires a complete immutable snapshot, all RunState path/hash pairs, valid start/end drift, every frozen target resolved, zero manual/source-hash review, and zero pending or elapsed-but-unretried membership waits. `complete=True` only when every required scope gate passes; warnings/info do not block.

- [ ] **Step 3: Build reproducible verification trees, not descriptive mocks**

  `complete.json` is a deterministic scenario descriptor with exact run/category/work/source/membership/status/translation/archive/quarantine rows and requested link method; `incomplete.json` lists named single mutations and expected issue codes. `tests/library_tree_helpers.py::build_library_tree(tmp_path,descriptor)` uses `write_minimal_pdf`, the production object/materializer functions, typed atomic manifest writers and renderer to create actual object bytes, hardlinks/relative symlinks, attempt histories, run/snapshot/state/drift documents, translation catalogs, archive/quarantine manifests and HTML/CSV/JSON/Markdown. It returns typed paths/IDs so tests mutate one real layer at a time.

- [ ] **Step 4: Write failing invariant and negative regression tests**

  Cover: snapshot/category membership equality; config-only categories; extraction evidence; object SHA-256 set equality with the global object manifest; `Membership.local_path` equality with active category paths; correct link targets; PDF header/size/SHA-1-when-known/SHA-256/pypdf; no `.part` or HTML PDF; archive/quarantine/migration tree equality with their own manifests; zero manual review; no pending membership waits; supported terminal evidence; no missing translations; and complete `file://` links.

  Parametrize every code from Step 2 using one actual-tree mutation. Also cover pending SourceHashReview, corrupt RunState snapshot/start/end digests, terminal evidence hash/time, elapsed-but-unretried wait, wrong/out-of-root category-set manifest and a staging tree that differs from its report. Assert aggregation reports all independent errors, scope-specific immutable paths never overwrite each other, staging facts persist the exact logical-tree digest consumed by the journal, report self-hash validates, and identical input/clock produces byte-identical output. Re-run an unchanged staging/category scope with a later clock and assert it fully rechecks then reuses the same canonical bytes/path. Separately tamper with object bytes, a membership link, a render owned file, terminal evidence bytes and a backup/quarantine file while leaving their manifest bytes unchanged; every case must refuse reuse and create a new incomplete canonical report while the old referenced file remains intact.

- [ ] **Step 5: Run tests and confirm failure**

  Run: `python -m pytest tests/test_verify.py -q`

  Expected: missing verification enum/request/helpers/module.

- [ ] **Step 6: Implement composable scope-aware checks**

  Each check returns `VerificationIssue(code,severity,stable_ids,evidence)` without stopping at first failure. Resolve the scope to exact typed manifests before checking and reject paths outside root. Atomically write the deterministic scope report only after computing its canonical digest. A full-library report may include post-snapshot drift only as warning and never mutates the frozen run.

- [ ] **Step 7: Implement the migration post-verification adapter**

  Implement `make_migration_post_verify(root,run_id,category_name,clock)` returning the Chunk 2 callback `post_verify(final_path,deterministic_report_path)->report_sha256`. It builds a category-scope request whose metadata/object/output roots are the active library root and whose exact final category path must equal `final_path`, requires `complete=True`, freshly computes the logical final-tree hash with the same algorithm/exclusions as staging, and atomically writes exactly `metadata/verification/<run-id>-migration-post.json` with the schema fixed in Task 9. Reuse requires all Task 9 identity/digest checks. Test success, verifier failure, corrupt wrapper/reference, crash after wrapper before journal, and exactly-once callback reuse.

- [ ] **Step 8: Run tests, check whitespace and commit**

  Run: `python -m pytest tests/test_verify.py -q`

  Expected: complete fixture passes and every mutated fixture fails for the intended invariant.

  Run: `git diff --check`

  Commit:

  ```bash
  git add scripts/imslp_library/enums.py scripts/imslp_library/models.py scripts/imslp_library/verify.py tests/model_helpers.py tests/library_tree_helpers.py tests/test_verify.py tests/fixtures/verification
  git commit -m "feat: enforce full library completion invariants"
  ```

### Task 13: Expose guarded CLI workflows

**Files:**
- Create: `scripts/imslp_library/cli.py`
- Create: `scripts/imslp_library/workflows.py`
- Create: `scripts/imslp_library/capacity.py`
- Create: `tests/test_cli.py`
- Create: `tests/test_capacity.py`
- Create: `scripts/library.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Lock command/service boundaries and exact entry points**

  `pyproject.toml` adds `[project.scripts] imslp-library = "imslp_library.cli:main"`. `scripts/library.py` is exactly a thin import plus `raise SystemExit(main())` guard. `workflows.py` owns these orchestration services: `doctor_root`, `discover_categories`, `snapshot_run`, `extract_run`, `write_extraction_review`, `select_categories`, `capacity_run`, `download_run`, `translation_coverage`, `verify_legacy_root`, `dry_run_legacy_migration`, `stage_legacy_migration`, `activate_legacy_migration`, `recover_legacy_migration`, `render_run`, `verify_run`, and `status_run`. Each accepts typed/path arguments and injected client/transport/clock/environment dependencies where external state is touched; CLI only parses, calls one service and formats its result.

  `capacity.py` writes a canonical self-hashed report with exact fields `schema_version`, `run_id`, `config_sha256`, `snapshot_sha256`, `selection_sha256`, `category_names`, `workers`, `size_override_sha256s`, `generated_at`, `membership_count`, `unique_source_count`, `known_expected_bytes`, `unknown_size_count`, `unknown_size_source_ids`, `existing_verified_object_bytes`, `missing_known_bytes`, `max_missing_file_bytes`, `part_reserve_bytes`, `staging_metadata_reserve_bytes`, `retained_backup_bytes`, `current_volume_used_bytes`, `available_bytes`, `incremental_peak_bytes`, `safety_margin_bytes`, `required_free_bytes`, `projected_volume_used_bytes`, `pass`, `report_sha256`. Category names are unique sorted and hash back to selection; override digests are unique sorted. Immutable path is `metadata/runs/capacity/<run-id>/<selection-sha12>/w<workers>/<report_sha256>.json`; latest alias `metadata/runs/<run-id>-capacity-<selection-sha12>-w<workers>.json` may be atomically replaced and is never the recorded completion artifact.

  Project selected memberships to unique `source_id`. Reject inconsistent size/revision/hash metadata for one source. `known_expected_bytes` sums positive expected sizes once per unique source. A source is existing only when its recorded SHA-256 object path exists and passes byte/hash/PDF checks; `existing_verified_object_bytes` sums unique existing SHA-256 objects once. `missing_known_bytes` sums each non-existing unique source’s expected size (safe overestimate until content dedupe is known), and `max_missing_file_bytes` is its maximum. Workers are integer `1..2`; `part_reserve=workers*max_missing_file_bytes`; `staging_metadata_reserve=max(ceil(missing_known_bytes*0.02),1GiB)`; `incremental_peak=missing_known_bytes+part_reserve+staging_metadata_reserve`; `safety_margin=max(ceil(incremental_peak*0.20),1GiB)`; `required_free=incremental_peak+safety_margin`; projected used is current used plus incremental peak; pass requires zero unknown sizes and `available_bytes>=required_free_bytes`. Retained backups are reported but already included in current used, so they are not double-counted.

  An unknown expected size blocks. Resolve only with tracked `metadata/overrides/expected_sizes/<source-id>.json` fields `schema_version`, `source_id`, `page_revision_id`, positive `expected_size`, `evidence_path`, `evidence_sha256`, `reviewed_at`; validate exact source/revision/evidence and include the canonical override file digest in `size_override_sha256s`. Downloader requires an immutable passing report with matching config/snapshot, a category superset of its selection and at least its worker count, then fully revalidates override digests, remaining unique-source bytes/current free space before each batch.

  `translation_coverage` writes `metadata/runs/<run-id>-translation-coverage.json` with `schema_version`, `run_id`, `selection_sha256`, `composer_total`, `composer_resolved`, `composer_fallback`, `title_total`, `title_override`, `title_rule`, `title_fallback`, `missing_composers[{english,work_ids}]`, `missing_titles[{english,work_ids}]`, `complete`, `generated_at`. Completion requires every composer to have a reviewed non-fallback Chinese name and every title a nonempty override/rule/explicit fallback with source type; title fallback is allowed by the approved design.

  Stage orchestration runs audit → verified archive → object/materialized staging with typed legacy-import attempts → copy exact approved config and translation catalogs into the self-contained staging tree → build a direct-category selection → dataset from staging metadata → `render_library(staging,dataset,layout="category_root")` → `VerificationScope.STAGING`; only a complete `VerificationArtifact` canonical path/self-hash and its report facts tree hash can populate the journal fields and advance to `staging_verified`. Activation derives and validates staging path `_migration/<run-id>/For 3 guitars (arr)` from root/run ID and journal—there is no user-supplied staging path—then passes `make_migration_post_verify(...)` to the journaled activator. `verify-legacy` calls the read-only source-integrity service, not the migration mutator.

- [ ] **Step 2: Write failing CLI contract and guard tests**

  Assert `--root` resolves to an explicit absolute path; mutating commands reject `/`, a home directory, missing config or unsupported link capabilities. Cover `doctor`, `verify-legacy`, `discover`, `snapshot`, `extract`, `review-extraction`, `select-categories`, `capacity`, `download`, `translation-coverage`, `migrate-legacy --dry-run|--stage|--activate`, `recover-migration`, `render`, `verify` and `status` through both wrapper and console entry point.

  `snapshot` requires caller-supplied `--run-id`, `--migration-run-id`, `--all-approved`, and root-contained `--run-context metadata/operations/current-library-run.json`. Its first invocation also requires `--new-context`: before network access it validates the exact post-verified migration journal and atomically creates, without overwrite, exact fields `schema_version`, `run_id`, `migration_run_id`, `config_sha256`, `snapshot_path`, `state_path`, `status`, nullable `capacity_canonical_path`, nullable `capacity_report_sha256`, `created_at`, `updated_at`; `status` is `creating|paused|complete|failed`, and the capacity fields are an all-or-none pair initially null. The same-ID restart omits `--new-context`, reads and validates the pointer, and atomically updates status/timestamp. A mismatched ID/migration/config/path or pre-existing pointer blocks. After a passing capacity run, `capacity_run` fully revalidates its canonical artifact and atomically records that root-relative canonical path/hash pair in this same context; a failing report never changes the pair. Tests cover crash before snapshot, paused restart, successful completion, capacity-pair update, corruption, and refusal to replace even a completed pointer; a future run must first archive/remove the completed pointer through a separately designed command, not an ad hoc shell action. `discover` accepts `--phase standalone|start|end`; start/end require `--compare-run` and atomically update the corresponding RunState pair. `select-categories` requires run ID, `--preset pilot|wave-multi|wave-duo|wave-solo`, and a root-contained `--output`; pilot chooses the minimum `(member_count, UTF-8 name)` nonempty original and non-flexible arrangement plus required nonempty `For 2 and 3 guitars (arr)`. Wave-multi contains every nonempty approved category except exact four base solo/duo names, wave-duo contains the two base duo names, and wave-solo contains the two base solo names. Validation mode `select-categories --validate-wave-files <multi> <duo> <solo>` reads the three exact schemas and exits 0 only when they are pairwise disjoint and their union equals the frozen nonempty approved set. Output uses the exact CategorySelection file schema; tests cover generation and validation failures.

  `review-extraction` requires `--run-id`, exact `--category`, `--page-id`, and either `--source-id` or `--page-level`; only `--decision exclude --reason <nonblank>` is accepted. It loads the current exact manual-review `ExtractionDecision` from the frozen extraction artifact, verifies the requested identifiers and evidence revision, hashes that decision with the production canonical serializer, and computes category SHA-256 from normalized UTF-8. The only output is `metadata/overrides/extraction_reviews/<run-id>/<category-sha10>-p<page-id>-<source-id-or-page>.json`. It creates the typed `ExtractionReview` atomically without overwrite, re-reads and validates it, and refuses any request that would include or broaden scope. Duplicate identical commands return the same validated artifact; conflicting content blocks.

  `capacity` requires `--run-id`, one selector and `--workers 1|2`. `download` requires the same selector, `--workers 1|2`, a matching passing capacity gate (all-approved gate may authorize a subset with the same run/config and no larger worker count), and a complete frozen run. `translation-coverage`, `render`, and `status` accept the same selector flags. `verify` requires `--scope staging|category|categories-file|all-approved`; staging derives the path/selector, category requires `--category`, categories-file requires `--categories-file`, and all-approved accepts neither. Activation requires exact root/legacy/run ID plus `--confirm-staging-sha256`, whose value must match the complete staging canonical report self-hash and journal.

  Final `status` additionally accepts `--migration-run-id` plus root-contained `--write-completion-record`. It first fully revalidates the immutable all-approved verification artifact and capacity artifact, migration journal/post wrapper, config/snapshot/RunState drift hashes, referenced tracked overrides and counts. It builds exact fields `schema_version`, `migration_run_id`, `library_run_id`, `config_sha256`, `snapshot_sha256`, `start_drift_report_sha256`, `end_drift_report_sha256`, `capacity_canonical_path`, `capacity_report_sha256`, `capacity_pass:true`, `verification_canonical_path`, `verification_report_sha256`, `verification_complete:true`, `migration_post_path`, `migration_post_sha256`, `counts{categories,works,sources,memberships,objects,object_bytes,terminal_status_counts}`, `tracked_override_paths`, `created_at`, and `record_sha256`. `record_sha256` is SHA-256 of UTF-8 `json.dumps(fields_without_record_sha256,ensure_ascii=False,sort_keys=True,separators=(",",":"))`. The immutable no-overwrite record is `metadata/operations/completions/<library-run-id>/<record_sha256>.json`; only after re-reading it successfully may the requested `--write-completion-record` path be atomically replaced as a convenience alias. Override paths are normalized tracked root-relative paths actually referenced by these two runs. Any mismatch returns exit 5 and writes nothing.

- [ ] **Step 3: Run tests and confirm failure**

  Run: `python -m pytest tests/test_capacity.py tests/test_cli.py -q`

  Expected: missing CLI/workflow/wrapper/entry point.

- [ ] **Step 4: Implement workflow services and guarded command wiring**

  Implement the exact service compositions and selectors above. CLI constructs typed options, calls one service, prints a concise human summary to stderr and one canonical JSON envelope to stdout with exact top-level keys `schema_version:1`, `command`, nullable `run_id`, `status`, `artifacts`, `counts`, `detail`. Artifact keys are command-specific: snapshot exposes `snapshot_path,state_path,snapshot_sha256,run_context_path`; selector exposes `selection_path,selection_sha256`; capacity exposes `canonical_path,latest_alias_path,report_sha256,pass`; verify exposes `canonical_path,latest_alias_path,report`; stage exposes nested `staging_verification` with the same verification artifact fields; activation exposes `journal_path,post_verification_path,post_verification_sha256`; coverage exposes `coverage_path,complete`. Use exit code `0` for success, `2` for invalid input/environment (including insufficient capacity), `3` for human verification pause, `4` for incomplete/manual-review state (including unknown expected sizes or incomplete translations) and `5` for verification failure. Capacity exit `0` has `status=success,pass=true`; capacity exit `2/4` still writes and exposes its immutable report with `status=incomplete,pass=false`. Coverage exit `0` has `status=success,complete=true`; coverage exit `4` has `status=incomplete,complete=false`. Tests parse stdout, reject extra/missing keys and inject fakes so no command performs live network or production-root writes.

  Lock the remaining command-specific dictionaries—unknown/extra keys fail tests:

  | Command | `artifacts` exact keys | `counts` exact keys | `detail` required keys |
  |---|---|---|---|
  | `doctor` | `doctor_report` | `available_bytes,required_free_bytes` | `volume_name,link_method` |
  | `verify-legacy` | `legacy_report,legacy_tree_sha256` | `work_pages,pdf_files,integrity_errors` | `legacy_path` |
  | `discover` | `report_path,report_sha256` | `new_candidates,empty_categories,deleted_categories,member_count_changes,possible_renames` | `phase` |
  | `extract` | `works_path,score_files_path,memberships_path,exclusions_path,manual_review_path` | `works,sources,memberships,excluded,manual_review_count` | `run_status` |
  | `review-extraction` | `review_path,review_sha256` | `reviews_written` | `category_name,page_id,source_id` |
  | `select-categories --preset ...` | `selection_path,selection_sha256` | `categories,memberships` | `preset` |
  | `select-categories --validate-wave-files` | `validated_paths,partition_sha256` | `multi,duo,solo,total` | `validation` |
  | `download` | `attempt_manifest_path` | `requested,verified,terminal,pending,manual_review,wait_pending` | `run_status` |
  | `translation-coverage` | `coverage_path,complete` | `composer_total,composer_resolved,composer_fallback,title_total,title_override,title_rule,title_fallback,missing_composers,missing_titles` | `selection_sha256` |
  | `render` | `render_manifest_path,render_manifest_sha256` | `categories,works,sources,memberships` | `layout` |
  | `migrate-legacy --dry-run` | `audit_path,audit_sha256` | `input,mixed_excluded,work_level_review,manual_review_count,integrity_error_count,eligible` | `journal_state` |
  | `recover-migration` | `journal_path,journal_sha256` | none | `journal_state,recovery_action` |
  | library `status` | `run_state_path` plus optional `completion_record_path,completion_record_alias` | `verified,terminal,pending,manual_review,wait_pending` | `run_status,journal_state` (`journal_state:null` without migration ID) |
  | migration-only `status` | `journal_path` | none | `run_status,journal_state` (`run_status:null`) |

  Snapshot/selector/capacity/verify/stage/activation/coverage use the artifact keys already fixed above and task-specific count keys from their typed reports. Every operational plan assertion below reads only these fields.

- [ ] **Step 5: Add exact destructive-operation guards**

  `--activate` requires run ID, exact legacy name and `--confirm-staging-sha256 <digest>`. Workflow derives staging/final/retired paths, confirms every path against the journal, checks the complete staging report’s path/self-hash/tree digest, and refuses mismatch or occupied destination. This guards target identity, not user intent: execution is already authorized by the approved specification.

- [ ] **Step 6: Run tests, check whitespace and commit**

  Run: `python -m pytest tests/test_capacity.py tests/test_cli.py -q`

  Expected: all CLI tests pass using only temporary roots and fake transports.

  Run: `git diff --check`

  Commit:

  ```bash
  git add pyproject.toml scripts/library.py scripts/imslp_library/capacity.py scripts/imslp_library/cli.py scripts/imslp_library/workflows.py tests/test_capacity.py tests/test_cli.py
  git commit -m "feat: add guarded IMSLP library CLI"
  ```

### Task 14: Integrate documentation and run the complete offline test suite

**Files:**
- Modify: `README.md`
- Modify: `TODO.md`
- Modify: `AGENTS.md`
- Create: `docs/operations.md`

- [ ] **Step 1: Document exact setup and recovery commands**

  Add `python -m pip install -e '.[test]'`, required `node --version` for test execution, command examples with `--root /Volumes/PHILIPS/programs/muse-cache/imslp`, output locations, scope-specific verification paths, exit codes, bot-check pause behavior, membership-wait resume, migration rollback and the difference between downloaded, terminally unavailable and excluded scores.

- [ ] **Step 2: Update project status without overstating completion**

  Mark software implementation complete only after tests pass. Keep metadata/download/migration/population tasks open. State that no operational run result exists yet.

- [ ] **Step 3: Run the entire test suite and static checks**

  Run:

  ```bash
  python -m pytest -q
  python -m compileall -q scripts tests
  git diff --check
  git status --short
  ```

  Expected: all tests pass; compileall emits no errors; diff check passes; status contains only the intended documentation changes before commit.

- [ ] **Step 4: Commit Chunk 3**

  ```bash
  git add README.md TODO.md AGENTS.md docs/operations.md
  git commit -m "docs: add IMSLP library operation runbook"
  ```

## Chunk 4: Production migration, staged downloads, and final acceptance

Execute this entire chunk in one dedicated `zsh` session beginning with `set -euo pipefail`. Every code block is a gate: a nonzero block stops execution and mechanically prevents all later steps. Commands expected to return controlled pause/incomplete codes use an explicit `if ...; then ...; else CODE=$?; fi` branch, validate that code and envelope, and return/stop before any later task. After a lost shell, re-enable `set -euo pipefail` and recover only the persisted run identities described below.

### Task 15: Preflight and migrate `for3guitars` through verified staging

**Files:**
- Generate: `metadata/migrations/<run-id>.json`
- Generate: `metadata/migrations/<run-id>-legacy-audit.json`
- Generate: `backups/manifests/for3guitars-pre-migration-<run-id>.json`
- Generate: `quarantine/legacy-mixed-<run-id>/manifest.json`
- Generate: `_migration/<run-id>/For 3 guitars (arr)/`
- Activate: `For 3 guitars (arr)/`

- [ ] **Step 1: Create and keep a migration-only run identity**

  In one persistent shell session, create a collision-checked identity distinct from any later snapshot run:

  ```bash
  set -euo pipefail
  MIGRATION_RUN_ID="migration-$(python -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))')"
  test ! -e "/Volumes/PHILIPS/programs/muse-cache/imslp/metadata/migrations/${MIGRATION_RUN_ID}.json"
  ```

  Keep `MIGRATION_RUN_ID` for every dry-run, override, stage, recovery and activation command. If the shell is lost, recover the exact ID from the single audit/journal path already created; never generate a replacement for an in-progress migration.

- [ ] **Step 2: Run target-volume and immutable legacy preflight**

  ```bash
  DOCTOR_JSON="$(python scripts/library.py doctor --root /Volumes/PHILIPS/programs/muse-cache/imslp)"
  LEGACY_JSON="$(python scripts/library.py verify-legacy --root /Volumes/PHILIPS/programs/muse-cache/imslp --legacy for3guitars)"
  python -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="success" and d["detail"]["volume_name"]=="PHILIPS" and d["counts"]["available_bytes"]>=d["counts"]["required_free_bytes"]' <<<"${DOCTOR_JSON}"
  python -c 'import json,sys; d=json.load(sys.stdin); c=d["counts"]; assert d["status"]=="success" and c=={"work_pages":456,"pdf_files":1451,"integrity_errors":0}' <<<"${LEGACY_JSON}"
  ```

  The current source boundary is 456 work pages and 1,451 structurally/hash-valid PDFs; any difference from the remeasured tree blocks migration and triggers investigation rather than editing expected counts.

- [ ] **Step 3: Run and validate the strict dry-run under that same ID**

  ```bash
  if MIGRATION_DRY_JSON="$(python scripts/library.py migrate-legacy --dry-run --root /Volumes/PHILIPS/programs/muse-cache/imslp --legacy for3guitars --run-id "${MIGRATION_RUN_ID}")"; then
    MIGRATION_DRY_EXIT=0
  else
    MIGRATION_DRY_EXIT=$?
  fi
  test "${MIGRATION_DRY_EXIT}" -eq 4
  python -c 'import json,sys; d=json.load(sys.stdin); assert d["run_id"]==sys.argv[1] and d["status"]=="incomplete"; assert d["counts"]["input"]==1451 and d["counts"]["mixed_excluded"]==6 and d["counts"]["work_level_review"]==77' "${MIGRATION_RUN_ID}" <<<"${MIGRATION_DRY_JSON}"
  ```

  Expected: no backup, active object, staging tree or rename; eligible upper bound is 1,445 and the exact audit path is `metadata/migrations/${MIGRATION_RUN_ID}-legacy-audit.json`.

- [ ] **Step 4: Resolve all work-level records with the exact override schema**

  Review all 77 rows against cached page/revision and raw/normalized `Instrumentation`. Create `metadata/overrides/work_level_review_${MIGRATION_RUN_ID}.json` with the Task 8 schema, source audit SHA-256 and one unique `review_id`-sorted decision per file row. Include only exact-instrumentation acceptance or evidence-backed exclusion; do not trust the old synthetic section label.

  Rerun dry-run with the same ID, but validate the ready state with this separate post-review assertion (do not reuse Step 3’s intentionally incomplete assertion):

  ```bash
  MIGRATION_READY_JSON="$(python scripts/library.py migrate-legacy --dry-run --root /Volumes/PHILIPS/programs/muse-cache/imslp --legacy for3guitars --run-id "${MIGRATION_RUN_ID}")"
  python -c 'import json,sys; d=json.load(sys.stdin); c=d["counts"]; assert d["run_id"]==sys.argv[1] and d["status"]=="success"; assert c["input"]==1451 and c["mixed_excluded"]==6 and c["work_level_review"]==77 and c["manual_review_count"]==0 and c["integrity_error_count"]==0' "${MIGRATION_RUN_ID}" <<<"${MIGRATION_READY_JSON}"
  ```

  The override contains 77 unique row-level `review_id` items even when several share a page. Audit/override hashes must match; a changed audit makes the override stale and blocks.

- [ ] **Step 5: Create verified backup/staging and capture the canonical artifact**

  ```bash
  MIGRATION_STAGE_JSON="$(python scripts/library.py migrate-legacy --stage --root /Volumes/PHILIPS/programs/muse-cache/imslp --legacy for3guitars --run-id "${MIGRATION_RUN_ID}")"
  STAGING_REPORT_SHA256="$(python -c 'import json,sys; d=json.load(sys.stdin); a=d["artifacts"]["staging_verification"]; assert a["report"]["complete"] is True; assert len(a["report"]["facts"]["staging_tree_sha256"])==64; print(a["report"]["report_sha256"])' <<<"${MIGRATION_STAGE_JSON}")"
  STAGING_CANONICAL_PATH="$(python -c 'import json,sys; print(json.load(sys.stdin)["artifacts"]["staging_verification"]["canonical_path"])' <<<"${MIGRATION_STAGE_JSON}")"
  ```

  Re-run and prove immutable reuse after full rechecking:

  ```bash
  STAGING_RECHECK_JSON="$(python scripts/library.py verify --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${MIGRATION_RUN_ID}" --scope staging)"
  python -c 'import json,sys; a=json.load(sys.stdin)["artifacts"]; assert a["canonical_path"]==sys.argv[1] and a["report"]["report_sha256"]==sys.argv[2] and a["report"]["complete"] is True' "${STAGING_CANONICAL_PATH}" "${STAGING_REPORT_SHA256}" <<<"${STAGING_RECHECK_JSON}"
  ```

  Archive equality, six-file mixed quarantine, eligible count, object links, virtual-final hrefs, report self-hash and logical-tree hash must all pass.

- [ ] **Step 6: Use the exact recovery command after any interruption**

  ```bash
  RECOVERY_JSON="$(python scripts/library.py recover-migration --root /Volumes/PHILIPS/programs/muse-cache/imslp --legacy for3guitars --run-id "${MIGRATION_RUN_ID}")"
  python -c 'import json,sys; d=json.load(sys.stdin); assert d["run_id"]==sys.argv[1] and d["detail"]["journal_state"] in {"planned","backup_verified","staging_verified","legacy_moved","category_activated","post_verified","rollback_required","rolled_back"}' "${MIGRATION_RUN_ID}" <<<"${RECOVERY_JSON}"
  MIGRATION_STATUS_JSON="$(python scripts/library.py status --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${MIGRATION_RUN_ID}")"
  ```

  For `planned|backup_verified|staging_verified`, rerun the interrupted dry-run/stage/activate command with the same ID. For `legacy_moved|category_activated`, recovery reconciles the exact path matrix and continues post-verification. For `rollback_required`, rerun recovery until `rolled_back` or a reported occupied-path blocker is removed explicitly; after `rolled_back`, the old tree is active and a corrected migration uses a new ID. Never manually rename/delete migration paths.

- [ ] **Step 7: Activate using only the captured staging report self-hash**

  ```bash
  MIGRATION_ACTIVATE_JSON="$(python scripts/library.py migrate-legacy --activate --root /Volumes/PHILIPS/programs/muse-cache/imslp --legacy for3guitars --run-id "${MIGRATION_RUN_ID}" --confirm-staging-sha256 "${STAGING_REPORT_SHA256}")"
  python -c 'import json,sys; d=json.load(sys.stdin); assert d["run_id"]==sys.argv[1] and d["status"]=="success"; assert len(d["artifacts"]["post_verification_sha256"])==64' "${MIGRATION_RUN_ID}" <<<"${MIGRATION_ACTIVATE_JSON}"
  python scripts/library.py verify --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${MIGRATION_RUN_ID}" --scope category --category "For 3 guitars (arr)"
  ```

  Expected: journal is `post_verified`; the legacy tree is `backups/for3guitars-retired-${MIGRATION_RUN_ID}/`; the immutable migration-post wrapper still hashes its immutable category report after the repeated check; the new category opens locally and has no mixed-bass membership.

- [ ] **Step 8: Commit only tracked review/status evidence**

  Update README/TODO with measured counts, `MIGRATION_RUN_ID`, backup/quarantine paths, canonical staging report path/hash and migration-post path/hash. Commit the reviewed work-level override because overrides are tracked; bulk/generated trees remain ignored.

  ```bash
  git add README.md TODO.md "metadata/overrides/work_level_review_${MIGRATION_RUN_ID}.json"
  git diff --check
  python -c 'import subprocess,sys; expected={"README.md","TODO.md",f"metadata/overrides/work_level_review_{sys.argv[1]}.json"}; staged=set(subprocess.check_output(["git","diff","--cached","--name-only"],text=True).splitlines()); assert staged==expected' "${MIGRATION_RUN_ID}"
  git diff --cached --check
  git commit -m "docs: record verified three-guitar migration"
  test -z "$(git status --porcelain)"
  ```

  Expected: the staged set is exactly the two status documents and the run-bound override, both whitespace checks pass, the commit succeeds, and only ignored generated data remains.

### Task 16: Freeze all approved categories and produce the capacity gate

**Files:**
- Generate: `metadata/category_drift_report.json`
- Generate: `metadata/runs/<run-id>.json`
- Generate: `metadata/works.json`
- Generate: `metadata/score_files.json`
- Generate: `metadata/memberships.json`
- Generate: `metadata/path_map.json`

- [ ] **Step 1: Run discovery without modifying scope**

  ```bash
  ALLOWLIST_BEFORE="$(shasum -a 256 config/categories.json | awk '{print $1}')"
  DISCOVERY_JSON="$(python scripts/library.py discover --root /Volumes/PHILIPS/programs/muse-cache/imslp --phase standalone)"
  python -c 'import json,sys; d=json.load(sys.stdin); assert d["command"]=="discover" and d["run_id"] is None and d["status"]=="success" and d["detail"]["phase"]=="standalone"; assert len(d["artifacts"]["report_sha256"])==64 and d["artifacts"]["report_path"]=="metadata/category_drift_report.json"' <<<"${DISCOVERY_JSON}"
  ALLOWLIST_AFTER="$(shasum -a 256 config/categories.json | awk '{print $1}')"
  test "${ALLOWLIST_AFTER}" = "${ALLOWLIST_BEFORE}"
  ```

  Expected: report references allowlist version `2026-08-30.1`; `config/categories.json` hash is unchanged. Any newly discovered category remains report-only.

- [ ] **Step 2: Create a separate library run and freeze one complete snapshot**

  ```bash
  LIBRARY_RUN_ID="library-$(python -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))')"
  test "${LIBRARY_RUN_ID}" != "${MIGRATION_RUN_ID}"
  test ! -e "/Volumes/PHILIPS/programs/muse-cache/imslp/metadata/runs/${LIBRARY_RUN_ID}.json"
  test ! -e "/Volumes/PHILIPS/programs/muse-cache/imslp/metadata/runs/${LIBRARY_RUN_ID}-state.json"
  test ! -e "/Volumes/PHILIPS/programs/muse-cache/imslp/metadata/operations/current-library-run.json"
  if SNAPSHOT_JSON="$(python scripts/library.py snapshot --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --migration-run-id "${MIGRATION_RUN_ID}" --all-approved --run-context metadata/operations/current-library-run.json --new-context)"; then
    SNAPSHOT_EXIT=0
  else
    SNAPSHOT_EXIT=$?
  fi
  if [ "${SNAPSHOT_EXIT}" -eq 3 ]; then
    python -c 'import json,sys; d=json.load(sys.stdin); assert d["run_id"]==sys.argv[1] and d["status"]=="paused" and d["artifacts"]["run_context_path"]=="metadata/operations/current-library-run.json"' "${LIBRARY_RUN_ID}" <<<"${SNAPSHOT_JSON}"
    exit 3
  fi
  test "${SNAPSHOT_EXIT}" -eq 0
  python -c 'import json,sys; d=json.load(sys.stdin); a=d["artifacts"]; assert d["run_id"]==sys.argv[1] and d["status"]=="success"; assert len(a["snapshot_sha256"])==64 and a["run_context_path"]=="metadata/operations/current-library-run.json"' "${LIBRARY_RUN_ID}" <<<"${SNAPSHOT_JSON}"
  ```

  Exit `3` stops the shell before Step 3. Complete the human verification, then run this exact resume gate; repeat it after each further exit `3`:

  ```bash
  set -euo pipefail
  LIBRARY_RUN_ID="$(python -c 'import json; d=json.load(open("metadata/operations/current-library-run.json")); assert d["status"] in {"creating","paused","complete","failed"}; print(d["run_id"])')"
  MIGRATION_RUN_ID="$(python -c 'import json; d=json.load(open("metadata/operations/current-library-run.json")); print(d["migration_run_id"])')"
  if SNAPSHOT_JSON="$(python scripts/library.py snapshot --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --migration-run-id "${MIGRATION_RUN_ID}" --all-approved --run-context metadata/operations/current-library-run.json)"; then
    SNAPSHOT_EXIT=0
  else
    SNAPSHOT_EXIT=$?
  fi
  if [ "${SNAPSHOT_EXIT}" -eq 3 ]; then
    python -c 'import json,sys; d=json.load(sys.stdin); assert d["run_id"]==sys.argv[1] and d["status"]=="paused" and d["artifacts"]["run_context_path"]=="metadata/operations/current-library-run.json"' "${LIBRARY_RUN_ID}" <<<"${SNAPSHOT_JSON}"
    exit 3
  fi
  test "${SNAPSHOT_EXIT}" -eq 0
  python -c 'import json,sys; d=json.load(sys.stdin); a=d["artifacts"]; assert d["run_id"]==sys.argv[1] and d["status"]=="success" and len(a["snapshot_sha256"])==64; assert a["snapshot_path"]==f"metadata/runs/{sys.argv[1]}.json" and a["state_path"]==f"metadata/runs/{sys.argv[1]}-state.json" and a["run_context_path"]=="metadata/operations/current-library-run.json"' "${LIBRARY_RUN_ID}" <<<"${SNAPSHOT_JSON}"
  ```

  The complete snapshot contains every nonempty approved category, exact page/revision/file metadata and config hash. Validate the pointer’s config hash and exact snapshot/RunState paths before continuing. Never search a glob or mint a competing ID.

- [ ] **Step 3: Capture immutable run-start drift and bind it to RunState**

  ```bash
  START_DRIFT_JSON="$(python scripts/library.py discover --root /Volumes/PHILIPS/programs/muse-cache/imslp --compare-run "${LIBRARY_RUN_ID}" --phase start)"
  python -c 'import json,sys; d=json.load(sys.stdin); a=d["artifacts"]; assert d["run_id"]==sys.argv[1] and d["status"]=="success" and d["detail"]["phase"]=="run_start"; assert a["report_path"]==f"metadata/runs/{sys.argv[1]}-category-drift-start.json" and len(a["report_sha256"])==64' "${LIBRARY_RUN_ID}" <<<"${START_DRIFT_JSON}"
  ```

  Expected: `metadata/runs/${LIBRARY_RUN_ID}-category-drift-start.json` exists, its path/hash pair is in RunState, and snapshot/config bytes are unchanged.

- [ ] **Step 4: Extract strict memberships and clear manual review**

  ```bash
  if EXTRACT_JSON="$(python scripts/library.py extract --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}")"; then
    EXTRACT_EXIT=0
  else
    EXTRACT_EXIT=$?
  fi
  { [ "${EXTRACT_EXIT}" -eq 0 ] || [ "${EXTRACT_EXIT}" -eq 4 ]; }
  python -c 'import json,sys; d=json.load(sys.stdin); a=d["artifacts"]; assert d["command"]=="extract" and d["run_id"]==sys.argv[1] and d["status"] in {"success","incomplete"}; assert set(a)=={"works_path","score_files_path","memberships_path","exclusions_path","manual_review_path"}; assert d["counts"]["manual_review_count"]>=0' "${LIBRARY_RUN_ID}" <<<"${EXTRACT_JSON}"
  MANUAL_REVIEW_PATH="$(python -c 'import json,sys; print(json.load(sys.stdin)["artifacts"]["manual_review_path"])' <<<"${EXTRACT_JSON}")"
  ```

  Inspect every row in `${MANUAL_REVIEW_PATH}` against its saved exact-revision evidence. If a row should match an approved rule, fix and test extraction code instead of overriding inclusion. For an evidence-backed exclusion, set `CATEGORY_NAME`, `PAGE_ID`, optional `SOURCE_ID`, and a nonblank `REVIEW_REASON` from that exact row, then run exactly one of:

  ```bash
  REVIEW_JSON="$(python scripts/library.py review-extraction --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --category "${CATEGORY_NAME}" --page-id "${PAGE_ID}" --source-id "${SOURCE_ID}" --decision exclude --reason "${REVIEW_REASON}")"
  # Page-level alternative, used only when the source ID in the row is null:
  # REVIEW_JSON="$(python scripts/library.py review-extraction --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --category "${CATEGORY_NAME}" --page-id "${PAGE_ID}" --page-level --decision exclude --reason "${REVIEW_REASON}")"
  python -c 'import json,sys; d=json.load(sys.stdin); a=d["artifacts"]; assert d["status"]=="success" and d["counts"]=={"reviews_written":1}; assert len(a["review_sha256"])==64 and a["review_path"].startswith(f"metadata/overrides/extraction_reviews/{sys.argv[1]}/")' "${LIBRARY_RUN_ID}" <<<"${REVIEW_JSON}"
  ```

  The command derives the canonical decision digest and filename; do not hand-author either. After all exact rows are resolved, rerun and enforce readiness:

  ```bash
  EXTRACT_READY_JSON="$(python scripts/library.py extract --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}")"
  python -c 'import json,sys; d=json.load(sys.stdin); c=d["counts"]; assert d["run_id"]==sys.argv[1] and d["status"]=="success" and c["manual_review_count"]==0; assert c["works"]>0 and c["sources"]>0 and c["memberships"]>0' "${LIBRARY_RUN_ID}" <<<"${EXTRACT_READY_JSON}"
  ```

  Mixed branches remain excluded and no review can broaden scope.

- [ ] **Step 5: Generate and enforce the persisted full-run capacity gate**

  ```bash
  if CAPACITY_JSON="$(python scripts/library.py capacity --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --all-approved --workers 2)"; then
    CAPACITY_EXIT=0
  else
    CAPACITY_EXIT=$?
  fi
  if [ "${CAPACITY_EXIT}" -ne 0 ]; then
    { [ "${CAPACITY_EXIT}" -eq 2 ] || [ "${CAPACITY_EXIT}" -eq 4 ]; }
    python -c 'import json,sys; d=json.load(sys.stdin); assert d["run_id"]==sys.argv[1] and d["status"]=="incomplete" and d["artifacts"]["pass"] is False; assert len(d["artifacts"]["report_sha256"])==64' "${LIBRARY_RUN_ID}" <<<"${CAPACITY_JSON}"
    exit "${CAPACITY_EXIT}"
  fi
  python -c 'import json,sys; d=json.load(sys.stdin); assert d["run_id"]==sys.argv[1] and d["artifacts"]["pass"] is True' "${LIBRARY_RUN_ID}" <<<"${CAPACITY_JSON}"
  CAPACITY_CANONICAL_PATH="$(python -c 'import json,sys; print(json.load(sys.stdin)["artifacts"]["canonical_path"])' <<<"${CAPACITY_JSON}")"
  CAPACITY_REPORT_SHA256="$(python -c 'import json,sys; print(json.load(sys.stdin)["artifacts"]["report_sha256"])' <<<"${CAPACITY_JSON}")"
  python -c 'import json,sys; d=json.load(open("metadata/operations/current-library-run.json")); assert d["run_id"]==sys.argv[1] and d["capacity_canonical_path"]==sys.argv[2] and d["capacity_report_sha256"]==sys.argv[3]' "${LIBRARY_RUN_ID}" "${CAPACITY_CANONICAL_PATH}" "${CAPACITY_REPORT_SHA256}"
  ```

  Exit `2/4` stops before Task 17 but preserves the failing immutable report. Resolve unknown sizes only with tracked exact-revision size overrides, recover both IDs from `metadata/operations/current-library-run.json`, and rerun this exact gate until exit `0`. Retain the immutable canonical capacity path/hash from stdout, and require its latest alias `metadata/runs/${LIBRARY_RUN_ID}-capacity-<selection-sha12>-w2.json`, matching config/snapshot/all-approved categories, exact size-override digest set, `unknown_size_count=0`, formula equality and `available_bytes>=required_free_bytes`. A corrected gate creates a new canonical report and atomically updates the context pair. This gate authorizes later subset waves at no more than two workers; downloader still recomputes remaining/free-space safety before every batch.

- [ ] **Step 6: Restore and revalidate all production variables after every shell restart**

  Run this gate after any later pilot, wave, coverage or other pause opens a new shell. It restores both IDs from the single exact context and reruns capacity, so the completion variables never depend on shell history:

  ```bash
  set -euo pipefail
  LIBRARY_RUN_ID="$(python -c 'import json; d=json.load(open("metadata/operations/current-library-run.json")); assert d["status"]=="complete"; print(d["run_id"])')"
  MIGRATION_RUN_ID="$(python -c 'import json; d=json.load(open("metadata/operations/current-library-run.json")); print(d["migration_run_id"])')"
  test -f "metadata/migrations/${MIGRATION_RUN_ID}.json"
  if CAPACITY_JSON="$(python scripts/library.py capacity --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --all-approved --workers 2)"; then
    CAPACITY_EXIT=0
  else
    CAPACITY_EXIT=$?
  fi
  if [ "${CAPACITY_EXIT}" -ne 0 ]; then
    { [ "${CAPACITY_EXIT}" -eq 2 ] || [ "${CAPACITY_EXIT}" -eq 4 ]; }
    python -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="incomplete" and d["artifacts"]["pass"] is False' <<<"${CAPACITY_JSON}"
    exit "${CAPACITY_EXIT}"
  fi
  CAPACITY_CANONICAL_PATH="$(python -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="success" and d["artifacts"]["pass"] is True; print(d["artifacts"]["canonical_path"])' <<<"${CAPACITY_JSON}")"
  CAPACITY_REPORT_SHA256="$(python -c 'import json,sys; print(json.load(sys.stdin)["artifacts"]["report_sha256"])' <<<"${CAPACITY_JSON}")"
  python -c 'import json,sys; d=json.load(open("metadata/operations/current-library-run.json")); assert d["run_id"]==sys.argv[1] and d["migration_run_id"]==sys.argv[2]; assert d["capacity_canonical_path"]==sys.argv[3] and d["capacity_report_sha256"]==sys.argv[4]' "${LIBRARY_RUN_ID}" "${MIGRATION_RUN_ID}" "${CAPACITY_CANONICAL_PATH}" "${CAPACITY_REPORT_SHA256}"
  ```

### Task 17: Validate a small original/arrangement pilot end to end

**Files:**
- Generate: `metadata/runs/<library-run-id>-pilot-categories.json`
- Generate: selected small category directories and runtime metadata

- [ ] **Step 1: Select pilot categories deterministically**

  ```bash
  PILOT_FILE="metadata/runs/${LIBRARY_RUN_ID}-pilot-categories.json"
  PILOT_SELECT_JSON="$(python scripts/library.py select-categories --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --preset pilot --output "${PILOT_FILE}")"
  python -c 'import json,sys; d=json.load(sys.stdin); a=d["artifacts"]; assert d["run_id"]==sys.argv[1] and d["status"]=="success" and d["detail"]["preset"]=="pilot"; assert a["selection_path"]==sys.argv[2] and len(a["selection_sha256"])==64; assert d["counts"]["categories"]==3 and d["counts"]["memberships"]>0' "${LIBRARY_RUN_ID}" "${PILOT_FILE}" <<<"${PILOT_SELECT_JSON}"
  ```

  The preset chooses the minimum `(member_count, UTF-8 category name)` nonempty original and non-flexible arrangement, plus required nonempty `For 2 and 3 guitars (arr)`, then writes the exact run-bound schema/digest. Validate stdout and file hash; never use live counts.

- [ ] **Step 2: Download the pilot batch**

  Run:

  ```bash
  run_pilot() {
    PILOT_FILE="metadata/runs/${LIBRARY_RUN_ID}-pilot-categories.json"
    if PILOT_DOWNLOAD_JSON="$(python scripts/library.py download --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --categories-file "${PILOT_FILE}" --workers 2)"; then
      PILOT_EXIT=0
    else
      PILOT_EXIT=$?
    fi
    [ "${PILOT_EXIT}" -eq 0 ] || return "${PILOT_EXIT}"
    PILOT_RENDER_JSON="$(python scripts/library.py render --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --categories-file "${PILOT_FILE}")" || return $?
    PILOT_VERIFY_JSON="$(python scripts/library.py verify --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --scope categories-file --categories-file "${PILOT_FILE}")" || return $?
    python -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="success" and d["artifacts"]["report"]["complete"] is True' <<<"${PILOT_VERIFY_JSON}" || return 5
    PILOT_STATUS_JSON="$(python scripts/library.py status --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --categories-file "${PILOT_FILE}")" || return $?
  }
  run_pilot
  ```

  Expected: all obtainable pilot PDFs are verified; terminal unavailable items retain evidence; no pending membership wait is called complete; exact category directories and offline pages pass.

- [ ] **Step 3: Inspect pilot purity and deduplication evidence**

  Require the verification artifact `complete=True` and record its immutable canonical path/hash. Confirm every selected record has heading/instrumentation evidence, the flexible rule accepts no other `and`, and a repeated SHA-256 has one object plus every membership path. Stop rollout and fix/retest code if any invariant fails.

### Task 18: Download in risk-controlled category waves

**Files:**
- Generate: three mutually exclusive wave selection files
- Generate: category directories, object files, status and verification reports for the frozen run

- [ ] **Step 1: Generate and prove the three-wave partition**

  ```bash
  MULTI_FILE="metadata/runs/${LIBRARY_RUN_ID}-wave-multi.json"
  DUO_FILE="metadata/runs/${LIBRARY_RUN_ID}-wave-duo.json"
  SOLO_FILE="metadata/runs/${LIBRARY_RUN_ID}-wave-solo.json"
  MULTI_JSON="$(python scripts/library.py select-categories --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --preset wave-multi --output "${MULTI_FILE}")"
  DUO_JSON="$(python scripts/library.py select-categories --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --preset wave-duo --output "${DUO_FILE}")"
  SOLO_JSON="$(python scripts/library.py select-categories --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --preset wave-solo --output "${SOLO_FILE}")"
  python -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="success" and d["detail"]["preset"]==sys.argv[1] and d["artifacts"]["selection_path"]==sys.argv[2] and len(d["artifacts"]["selection_sha256"])==64 and d["counts"]["categories"]>0' wave-multi "${MULTI_FILE}" <<<"${MULTI_JSON}"
  python -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="success" and d["detail"]["preset"]==sys.argv[1] and d["artifacts"]["selection_path"]==sys.argv[2] and len(d["artifacts"]["selection_sha256"])==64 and d["counts"]["categories"]==2' wave-duo "${DUO_FILE}" <<<"${DUO_JSON}"
  python -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="success" and d["detail"]["preset"]==sys.argv[1] and d["artifacts"]["selection_path"]==sys.argv[2] and len(d["artifacts"]["selection_sha256"])==64 and d["counts"]["categories"]==2' wave-solo "${SOLO_FILE}" <<<"${SOLO_JSON}"
  PARTITION_JSON="$(python scripts/library.py select-categories --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --validate-wave-files "${MULTI_FILE}" "${DUO_FILE}" "${SOLO_FILE}")"
  python -c 'import json,sys; d=json.load(sys.stdin); a=d["artifacts"]; c=d["counts"]; assert d["status"]=="success" and d["detail"]["validation"]=="pairwise-disjoint-complete"; assert a["validated_paths"]==sys.argv[1:] and len(a["partition_sha256"])==64; assert c["multi"]+c["duo"]+c["solo"]==c["total"] and c["duo"]==c["solo"]==2' "${MULTI_FILE}" "${DUO_FILE}" "${SOLO_FILE}" <<<"${PARTITION_JSON}"
  ```

  Validate all three schemas/run IDs/digests, pairwise disjointness and union equality with the frozen nonempty approved-category set. `wave-multi` contains all extended-string, flexible, 3+ and ensemble/orchestra categories; duo/solo contain their exact two base categories.

- [ ] **Step 2: Execute each wave with the same exact loop**

  Run this real shell function; any nonzero download/render/verify/status or incomplete artifact returns immediately and mechanically prevents the next wave:

  ```bash
  run_wave_rollout() {
    for WAVE_NAME in wave-multi wave-duo wave-solo; do
      WAVE_FILE="metadata/runs/${LIBRARY_RUN_ID}-${WAVE_NAME}.json"
      if WAVE_DOWNLOAD_JSON="$(python scripts/library.py download --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --categories-file "${WAVE_FILE}" --workers 2)"; then
        WAVE_EXIT=0
      else
        WAVE_EXIT=$?
      fi
      if [ "${WAVE_EXIT}" -ne 0 ]; then
        python -c 'import json,sys; d=json.load(sys.stdin); assert d["status"] in {"paused","incomplete"}' <<<"${WAVE_DOWNLOAD_JSON}"
        return "${WAVE_EXIT}"
      fi
      python -c 'import json,sys; d=json.load(sys.stdin); assert d["command"]=="download" and d["status"]=="success" and d["counts"]["pending"]==0 and d["counts"]["manual_review"]==0 and d["counts"]["wait_pending"]==0' <<<"${WAVE_DOWNLOAD_JSON}" || return 5
      WAVE_RENDER_JSON="$(python scripts/library.py render --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --categories-file "${WAVE_FILE}")" || return $?
      WAVE_VERIFY_JSON="$(python scripts/library.py verify --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --scope categories-file --categories-file "${WAVE_FILE}")" || return $?
      python -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="success" and d["artifacts"]["report"]["complete"] is True' <<<"${WAVE_VERIFY_JSON}" || return 5
      WAVE_STATUS_JSON="$(python scripts/library.py status --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --categories-file "${WAVE_FILE}")" || return $?
      python -c 'import json,sys; d=json.load(sys.stdin); c=d["counts"]; assert d["status"]=="success" and c["pending"]==c["manual_review"]==c["wait_pending"]==0' <<<"${WAVE_STATUS_JSON}" || return 5
    done
  }
  run_wave_rollout
  ```

  Enter the next wave only when the current immutable verification artifact is `complete=True`. Existing pilot successes are skipped from the same frozen run; hashes reuse global objects. Exit 3 pauses for user human verification and resumes the same command/ID; exit 4 requires resolving waits/reviews before rerun; capacity failure returns exit 2/4 without starting more requests.

- [ ] **Step 3: Resolve every non-success state without changing snapshot scope**

  Retry transient and elapsed waits under the same wave selector. Leave only `downloaded_verified|source_override_verified` or evidence-backed permanent terminal states. `human_verification_required`, `manual_review`, unelapsed or elapsed-but-unretried waits, source conflicts, pending source-hash reviews and unexplained failures block that wave. Do not add live revisions/categories.

### Task 19: Finish translations, render the master library, and prove completion

**Files:**
- Generate: root `index.html`, `catalog.csv`, `score_manifest.csv`
- Generate: every approved nonempty category catalog
- Generate: `metadata/verification/<library-run-id>.json` latest alias and immutable canonical report
- Modify: `metadata/translations/composers_zh.json`
- Modify: `metadata/translations/title_overrides_zh.json`
- Modify: `metadata/translations/title_terms_zh.json`
- Modify: `README.md`
- Modify: `TODO.md`

- [ ] **Step 1: Complete bilingual references**

  ```bash
  if COVERAGE_REVIEW_JSON="$(python scripts/library.py translation-coverage --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --all-approved)"; then
    COVERAGE_REVIEW_EXIT=0
  else
    COVERAGE_REVIEW_EXIT=$?
  fi
  if [ "${COVERAGE_REVIEW_EXIT}" -eq 4 ]; then
    python -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="incomplete" and d["artifacts"]["complete"] is False; assert d["counts"]["composer_fallback"]>0 or d["counts"]["missing_composers"]>0 or d["counts"]["missing_titles"]>0' <<<"${COVERAGE_REVIEW_JSON}"
    exit 4
  fi
  test "${COVERAGE_REVIEW_EXIT}" -eq 0
  python -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="success" and d["artifacts"]["complete"] is True' <<<"${COVERAGE_REVIEW_JSON}"
  ```

  Exit `4` stops before rendering. Review `metadata/runs/${LIBRARY_RUN_ID}-translation-coverage.json`; add tracked composer mappings until `composer_fallback=0`, and add reviewed common title overrides/rules when reliable, otherwise keeping the explicit original-title fallback. In each new shell run Task 16 Step 6 first, then rerun this exact gate until exit `0`, `complete=True`, missing arrays are empty and every output has a source type.

  Final gate:

  ```bash
  COVERAGE_JSON="$(python scripts/library.py translation-coverage --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --all-approved)"
  python -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="success" and d["artifacts"]["complete"] is True and d["counts"]["composer_fallback"]==0 and d["counts"]["missing_composers"]==0 and d["counts"]["missing_titles"]==0' <<<"${COVERAGE_JSON}"
  ```

- [ ] **Step 2: Render all final static artifacts**

  Run: `python scripts/library.py render --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --all-approved`

  Expected: root and every nonempty category have HTML/Markdown/CSV/JSON outputs; search data is inline; no local `fetch()` exists; all links are relative and component-encoded.

- [ ] **Step 3: Capture end-of-run drift**

  Run: `python scripts/library.py discover --root /Volumes/PHILIPS/programs/muse-cache/imslp --compare-run "${LIBRARY_RUN_ID}" --phase end`

  Expected: additions/removals/revisions since snapshot are reported separately, `metadata/runs/${LIBRARY_RUN_ID}-category-drift-end.json` is immutable, its path/SHA-256 pair is stored in RunState, and the run snapshot remains unchanged.

- [ ] **Step 4: Run final full verification**

  Run:

  ```bash
  FINAL_VERIFY_JSON="$(python scripts/library.py verify --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --scope all-approved)"
  FINAL_CANONICAL_PATH="$(python -c 'import json,sys; d=json.load(sys.stdin); a=d["artifacts"]; assert a["report"]["complete"] is True; print(a["canonical_path"])' <<<"${FINAL_VERIFY_JSON}")"
  FINAL_REPORT_SHA256="$(python -c 'import json,sys; d=json.load(sys.stdin); print(d["artifacts"]["report"]["report_sha256"])' <<<"${FINAL_VERIFY_JSON}")"
  test "${#FINAL_REPORT_SHA256}" -eq 64
  python -m pytest -q
  python -m compileall -q scripts tests
  git diff --check
  ```

  Expected: verifier exit `0` and immutable canonical artifact is `complete=True`; object/membership/PDF/link/purity/translation/archive checks pass; review/wait queues are zero; all tests pass.

- [ ] **Step 5: Manually open the final entry**

  Open `file:///Volumes/PHILIPS/programs/muse-cache/imslp/index.html`. Search one English composer, one Chinese composer, one English title and one Chinese title; filter original/arrangement and at least two guitar counts; open a PDF from the root and a category page.

  Expected: every interaction works without an HTTP server and each PDF opens from its category-local path.

- [ ] **Step 6: Record measured completion and commit project status**

  First create the durable, fully validated completion record and assert it binds the variables captured above:

  ```bash
  COMPLETION_JSON="$(python scripts/library.py status --root /Volumes/PHILIPS/programs/muse-cache/imslp --run-id "${LIBRARY_RUN_ID}" --all-approved --migration-run-id "${MIGRATION_RUN_ID}" --write-completion-record "metadata/operations/${LIBRARY_RUN_ID}-completion.json")"
  COMPLETION_RECORD="$(python -c 'import json,sys; d=json.load(sys.stdin); a=d["artifacts"]; assert d["status"]=="success" and a["completion_record_alias"]==f"metadata/operations/{sys.argv[1]}-completion.json"; print(a["completion_record_path"])' "${LIBRARY_RUN_ID}" <<<"${COMPLETION_JSON}")"
  python -c 'import hashlib,json,sys; from pathlib import Path; p=Path(sys.argv[1]); d=json.load(open(p)); digest=d.pop("record_sha256"); encoded=json.dumps(d,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode("utf-8"); assert digest==hashlib.sha256(encoded).hexdigest()==p.stem; assert p.as_posix()==f"metadata/operations/completions/{sys.argv[3]}/{digest}.json"; assert d["migration_run_id"]==sys.argv[2] and d["library_run_id"]==sys.argv[3]; assert d["capacity_canonical_path"]==sys.argv[4] and d["capacity_report_sha256"]==sys.argv[5]; assert d["verification_canonical_path"]==sys.argv[6] and d["verification_report_sha256"]==sys.argv[7] and d["verification_complete"] is True' "${COMPLETION_RECORD}" "${MIGRATION_RUN_ID}" "${LIBRARY_RUN_ID}" "${CAPACITY_CANONICAL_PATH}" "${CAPACITY_REPORT_SHA256}" "${FINAL_CANONICAL_PATH}" "${FINAL_REPORT_SHA256}"
  ```

  Update README/TODO from this record with both IDs; config/snapshot/start/end drift hashes; exact category/member/work/score/object/byte and terminal counts; backup/quarantine paths; capacity artifact; and final immutable verification path/hash/`complete=True`. Do not cite the mutable latest alias, claim unavailable PDFs were downloaded, or call post-snapshot drift part of the frozen run.

  Generated root/catalog/category/render/runtime files are ignored by the Task 1 policy; tracked translation source JSON and any reviewed size/download/source-hash overrides must be committed. Check ignored/tracked policy before commit:

  ```bash
  git status --short --ignored
  git add README.md TODO.md metadata/translations/composers_zh.json metadata/translations/title_overrides_zh.json metadata/translations/title_terms_zh.json
  python -c 'import json,sys,subprocess; d=json.load(open(sys.argv[1])); [subprocess.run(["git","add","--",p],check=True) for p in d["tracked_override_paths"]]' "${COMPLETION_RECORD}"
  python -c 'import json,sys,subprocess; d=json.load(open(sys.argv[1])); allowed={"README.md","TODO.md","metadata/translations/composers_zh.json","metadata/translations/title_overrides_zh.json","metadata/translations/title_terms_zh.json",*d["tracked_override_paths"]}; staged=set(subprocess.check_output(["git","diff","--cached","--name-only"],text=True).splitlines()); assert staged <= allowed and {"README.md","TODO.md"} <= staged' "${COMPLETION_RECORD}"
  test -z "$(git diff --name-only)"
  git diff --cached --check
  git commit -m "docs: record verified IMSLP guitar library completion"
  test -z "$(git status --porcelain)"
  ```

  Expected: commit succeeds and the Git worktree is clean; generated library data remains present but ignored.
