# Legacy imports

This directory preserves user-supplied upstream artifacts used to reconstruct
the VoiceOver content pipeline.

- `source-archives/` contains immutable, local-only ZIP archives. ZIP files and
  processed audio are deliberately excluded from Git.
- `source-archives/manifest.json` records archive hashes and inventory facts.
- `staging/` is ignored scratch space for safe extraction and analysis.

Do not edit or recompress source archives. Derived manifests, reports, and
deployment packages must refer to the recorded SHA-256 value of their source.

The source archives are deliberately excluded from the default Docker build
context. Deployment tooling verifies a supplied archive against the tracked
manifest, then unpacks audio into a persistent data volume. This keeps Git and
the web application image free of processed audio while preserving a
reproducible import contract.
