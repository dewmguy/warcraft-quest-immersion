# Gated production phases

Work stops at the end of each phase until the owner explicitly approves the
result and authorizes the next phase. Producing a supporting artifact early,
such as the pronunciation dictionary, does not authorize work in a later
phase.

## Phase 1: Imported Sample — approved 2026-08-08

Purpose: establish the complete inherited audio baseline without altering its
payload.

Exit gate:

- preserve and hash every supplied source archive;
- reject unsafe or corrupt archives;
- inventory quest, gossip, lookup, and audio coverage;
- import audio into ignored persistent storage;
- validate every imported MP3 and duration lookup;
- compare the supplied interface with the maintained addon tree;
- identify unmatched dialogue, duplicate mappings, and remaining coverage
  questions;
- obtain owner approval of the Phase 1 report.

Approved result:

- 9,555 inherited MP3 files preserved outside Git and validated as decodable;
- 40.397 hours of exact-duration-matched audio;
- 6,558 quest recordings covering 3,685 unique quest IDs;
- 2,997 gossip recordings;
- source archives and interface comparison retained through hashed manifests and reports;
- 61 owner-reviewed Warcraft pronunciation entries normalized and approved.

## Phase 2: Placeholder Preview — in progress

Define and approve neutral race/gender voice profiles, a fixed evaluation
script, and a controlled set of delivery presets. Neutral voice identity is
approved before emotional delivery is evaluated.

Current review set:

- 46 draft profiles: narrator plus race IDs 1–22, each with male and female presentation;
- 33 combinations observed in the pinned display-data export;
- 11 deliberate fallback combinations and 2 required narrator profiles;
- neutral, angry, sorrowful, joyful, and proclaiming delivery presets;
- 230 planned profile/preset previews, all ungenerated;
- no attached source voices and no authorized paid API calls.

## Phase 3: Processed Text Output — pending

Build the suggestion-only text cleaning workflow. Original text remains
immutable; processed speech, omissions, delivery intent, pronunciation notes,
and revision history remain separately reviewable.

## Phase 4: Sample Pre-seed Preview — pending

Generate a short baseline-voice preview from a complete clause of approved
processed text. Display estimated usage before the owner authorizes the API
call and reuse identical cached results.

## Phase 5: Complete Pre-seed Preview — pending

Generate the complete baseline draft. Ordinary NPC audio can proceed toward
production approval; distinctive NPCs can enter the unique-profile branch.

## Phase 6: Sample Unique Seed Preview — pending

Create a versioned unique-profile fork with explicit parent lineage, source
clips, settings, and a short comparison preview.

## Phase 7: Complete Unique Seed Preview — pending

Generate and review the complete line using the approved unique profile.

## Phase 8: Production approval — pending

Promote the exact reviewed audio file without another generation call. Record
its immutable hash and release mapping.

## Phase 9: Addon packaging — pending

Compile approved audio, lookup tables, manifests, and the maintained interface
into client-specific packages and validate playback on each target client.
