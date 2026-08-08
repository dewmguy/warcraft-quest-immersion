# Gated project phases

These are milestones for building the project, not statuses attached to an
individual audio file. Work stops at the end of each project phase until the
owner explicitly approves the result and authorizes the next phase. The
independent per-profile and per-line workflows are defined in `WORKFLOW.md`.

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

## Phase 2: Voice Workbench — Dwarf proof of concept in progress

Define neutral race/gender voice profiles and build the interface that manages
their settings, evidence, gates, history, costs, and later audio execution.
Prove the interaction model with Dwarf Male and Dwarf Female before expanding
it across the complete matrix.

Current review set:

- 46 draft profiles: narrator plus race IDs 1–22, each with male and female presentation;
- 33 combinations observed in the pinned display-data export;
- 11 deliberate fallback combinations and 2 required narrator profiles;
- neutral, angry, sorrowful, joyful, and proclaiming delivery presets;
- 230 planned profile/preset previews, all ungenerated;
- no attached source voices and no authorized paid API calls.

The owner approved the Dwarf profile scope on 2026-08-08. The proof of concept
remains in no-audio mode: it simulates profile and line decisions without an
ElevenLabs request.

## Phase 3: Processed Text Tooling — pending

Build the suggestion-only text cleaning workflow. Original text remains
immutable; processed speech, omissions, delivery intent, pronunciation notes,
and revision history remain separately reviewable.

## Phase 4: Guarded short generation — pending

Generate a short baseline-voice preview from a complete clause of approved
processed text. Display estimated usage before the owner authorizes the API
call and reuse identical cached results.

## Phase 5: Complete baseline drafts — pending

Generate the complete baseline draft. Ordinary NPC audio can proceed toward
production approval; distinctive NPCs can enter the unique-profile branch.

## Phase 6: Unique voice forks — pending

Build the versioned unique-profile branch with explicit parent lineage, source
clips, settings, and short and complete comparison previews.

## Phase 7: Production review — pending

Promote the exact reviewed audio file without another generation call. Record
its immutable hash and release mapping.

## Phase 8: Addon packaging — pending

Compile approved audio, lookup tables, manifests, and the maintained interface
into client-specific packages and validate playback on each target client.
