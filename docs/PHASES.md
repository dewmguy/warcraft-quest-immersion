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

## Phase 2: Full-scope Alpha production database — in progress

The Dwarf proof of concept was rejected on 2026-08-08 because it exposed project
language instead of a clear content-production workflow. It has been retired.

The replacement Alpha works across the entire imported source and organizes the
real production records: dialogue, speakers, spoken-text revisions, baseline and
unique voices, reference clips, provider attempts, audio candidates, approvals,
and later addon-ready exports. Each workspace displays progress for 230 baseline
race/gender/delivery presets, quest audio, and gossip audio.

Exit gate:

- accept and version a complete quest and gossip export;
- replace the four-row demonstration source with a joined AzerothCore corpus;
- infer reviewable NPC role, affiliation, zone, story reach, and concise context;
- prepare missing quest spoken text deterministically during import while
  preserving existing reviewed revisions, and leave gossip preparation
  explicit;
- assign baseline or unique voices at the NPC level;
- keep delivery at the dialogue level;
- require separate confirmation for every provider request;
- approve an exact audio candidate without regeneration;
- export deterministic addon filenames, hashes, and durations;
- validate one real record through the existing addon packaging and playback path.

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
