# Alpha production model

The portal is a production database. It prepares approved audio assets for the
existing addon packaging and playback tooling; addon behavior is not a website
workflow.

## Controlled path

1. Import the complete validated quest and gossip source.
2. Find a dialogue record in the work queue.
3. Review the NPC, object, or item delivering it.
4. Click **Prepare spoken text** when a speech revision is wanted.
5. Review or edit that revision and assign the speaker's voice.
6. Select the line delivery.
7. Explicitly confirm one paid generation request.
8. Review the returned audio candidate.
9. Approve an exact candidate as the production asset.
10. Export approved audio and its deterministic addon manifest.

Import does not populate spoken text, generate voice candidates, create provider
voices, clone voices, or generate dialogue audio. Each operation requires a
separate user action.

## Primary records

- **Source snapshot:** content-addressed input, expansion, locale, and import count.
- **Dialogue:** immutable source text and stable addon identity.
- **Speaker:** inferred and reviewable role, affiliation, zone, story reach,
  concise context, and voice assignment.
- **Spoken-text revision:** separately created and never overwrites the source.
- **Voice:** logical baseline or NPC-specific speaker.
- **Voice version:** exact description, creation method, provider ID, model, settings, and delta.
- **Delivery preset:** one reviewed audio tag, stability value, and status for a
  race/gender/emotion combination.
- **Reference clip:** ignored audio with provenance that remains locally previewable.
- **Generation:** one explicit provider request and its exact inputs and usage snapshot.
- **Audio candidate:** one returned file with hash and duration.
- **Production asset:** the exact approved candidate and addon filename.

## Voice model

A baseline voice represents a stable race-and-gender speaker. Delivery such as
angry, sorrowful, joyful, or proclaiming belongs to a dialogue line and is not a
separate voice by default. A unique NPC voice inherits its current baseline as
context but becomes an independently versioned record.

The owner-facing creation paths are description-only Voice Design,
reference-guided Voice Design, and Instant Voice Cloning. All produce a reusable
provider voice ID. Provider model selection lives in general settings, not in
individual voice records.

The ElevenLabs API also calls an integer repeatability input a seed. That value
is not the reusable voice. This project uses **voice profile** for its versioned
race/gender or NPC record, **provider voice ID** for the created ElevenLabs
voice, and **delivery preset** for emotion-specific tags and stability.

Unchanged voice saves do not create versions. Historical versions list their
deltas and may be restored by creating a new current version.

## Storage boundary

The Git repository contains application code, schema logic, and reviewed static
configuration. The runtime SQLite database, reference clips, voice previews,
generated dialogue candidates, and production audio are stored below
`data/alpha/` and ignored by Git.

Uploaded sources are stored below `data/sources/` and are also ignored. Each
expansion/locale has one active replaceable snapshot; other active sources stay
online. Approved ZIP exports are separated by expansion and locale before the
addon-relative path so identical quest IDs cannot collide across clients.
