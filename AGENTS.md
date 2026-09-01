# Project instructions

This directory is a reproducible, source-attributed, offline IMSLP library for
classical/acoustic guitar categories, including pure-guitar and guitar chamber
instrumentation.

- Preserve each included IMSLP category name exactly as its directory name.
- Include pure-guitar categories configured in `config/categories.json` and
  mixed/chamber categories configured in `config/mixed_categories.json`. Do
  not infer scope from the mere presence of the word `guitar`.
- Exclude electric, bass, Hawaiian, steel, and slide guitar; voice/chorus;
  electronics/tape; large orchestra; and alternative-solo categories where
  guitar is only an `or` option.
- Group mixed categories as strings, woodwinds, brass, keyboard/free reed,
  plucked instruments, percussion, or mixed chamber ensemble.
- Treat original and `(arr)` categories separately. Original categories may
  use only original scores/parts; arrangement categories may use only the
  exact target instrumentation subsection in the page `FILES` area.
- Instrumentation matches must be fully anchored after Unicode/markup
  normalization. A target prefix followed by bass, voice, another instrument,
  `and`, `with`, a list, or an unconfigured `or` is not a match.
- Never accept HTML, CAPTCHA, login, error, partial, or non-PDF responses as
  scores. Validate PDF header, expected byte size, SHA-1, and parseability.
- Keep download and metadata work resumable. Use atomic `.part` files and stop
  politely when IMSLP requires human verification.
- Deduplicate physical PDFs by internal SHA-256 while preserving IMSLP SHA-1,
  category-local paths, and every source attribution. Verified objects are
  immutable; category views must not create duplicate PDF entities.
- Preserve IMSLP titles and musician attributions as source fields. Chinese
  names are reference translations and must never replace the originals.
- Generated catalogs and verification output must distinguish category
  memberships, unique works, manifest records, and unique physical PDFs.
- Do not claim the full library is complete until manifest/file equality,
  hashes, category purity, translations, and all local links have been freshly
  verified.
- Measure completion against a frozen run snapshot and approved category
  allowlist version; report upstream category drift separately.
- When extending the existing library, use the resumable production pipeline
  directly. Do not introduce additional specification or review gates unless
  scope, permissions, or destructive changes genuinely require them.
- Use `python`, not the system `python3`, for project commands.
- Keep `README.md`, `TODO.md`, and this file current as implementation proceeds.
- Do not commit PDFs, caches, partial downloads, logs, or generated bulk
  catalogs to Git.
