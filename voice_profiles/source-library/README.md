# Voice source library

This directory defines the durable provenance layer for voice reference material.
Audio files stay outside Git; only their reviewed metadata belongs in the repository.

## Rules

- Put source clips under `clips/<source_id>/` in the working deployment.
- Add one row per clip to `manifest.csv` before a clip is used to design or clone a voice.
- Preserve the original file. Derivatives receive their own rows and hashes.
- Record the speaker/character, origin, rights or fair-use rationale, and intended scope.
- A race baseline may reference several clips, but every generated voice version must retain
  the exact source IDs and settings that produced it.
- Never commit source clips, processed previews, complete drafts, or production audio.

The manifest intentionally begins empty. User-supplied reference material has not yet been
received, and no ElevenLabs voice IDs have been selected or created.
