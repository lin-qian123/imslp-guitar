# Project instructions

This directory is a reproducible, source-attributed, offline IMSLP library for
pure classical/acoustic guitar instrumentation categories.

- Preserve each included IMSLP category name exactly as its directory name.
- Include only categories configured in `config/categories.json`. Do not infer
  scope from the mere presence of the word `guitar`.
- Exclude electric guitar, bass guitar, voice, and any mixed-instrument
  category.
- Treat original and `(arr)` categories separately. Original categories may
  use only original scores/parts; arrangement categories may use only the
  exact target instrumentation subsection in the page `FILES` area.
- Never accept HTML, CAPTCHA, login, error, partial, or non-PDF responses as
  scores. Validate PDF header, expected byte size, SHA-1, and parseability.
- Keep download and metadata work resumable. Use atomic `.part` files and stop
  politely when IMSLP requires human verification.
- Deduplicate physical PDFs by content while preserving category-local paths
  and every IMSLP source attribution.
- Preserve IMSLP titles and musician attributions as source fields. Chinese
  names are reference translations and must never replace the originals.
- Generated catalogs and verification output must distinguish category
  memberships, unique works, manifest records, and unique physical PDFs.
- Do not claim the full library is complete until manifest/file equality,
  hashes, category purity, translations, and all local links have been freshly
  verified.
- Use `python`, not the system `python3`, for project commands.
- Keep `README.md`, `TODO.md`, and this file current as implementation proceeds.
- Do not commit PDFs, caches, partial downloads, logs, or generated bulk
  catalogs to Git.
