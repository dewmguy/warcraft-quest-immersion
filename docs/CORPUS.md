# 3.3.5 enUS Corpus

The certified corpus is extracted from the owner's versioned AzerothCore world
snapshot. The SQL dump, DBC exports, ZIP bundles, runtime SQLite database, and
audio stay beneath ignored `data/` storage. Git contains this contract, the
extractor, migrations, validation, addon compatibility code, and fixtures.

## Required source

Stage the dump beneath:

```text
data/sources/azerothcore/3.3.5/enUS/
```

The restored database must contain the AzerothCore world tables used by the
extractor and provenance-matched 3.3.5 exports of CreatureDisplayInfo,
CreatureDisplayInfoExtra, FactionTemplate, Faction, and AreaTable. The accepted
DBC table names are documented in `AzerothCoreCorpusExtractor.REQUIRED_TABLE_VARIANTS`.
Missing or unrecognized variants stop extraction.

The source database is an on-demand `warcraft-quest-source-db` container in the
`corpus-build` Compose profile. It has no published port and is not part of the
normal application stack.

```bash
scripts/restore-azerothcore-snapshot.sh \
  /opt/warcraft-quest-immersion/data/sources/azerothcore/3.3.5/enUS/world.sql.gz \
  /opt/warcraft-quest-immersion/data/sources/azerothcore/3.3.5/enUS/dbc.sql.gz
```

## Extract and certify

```bash
docker compose -f /home/plex/docker-compose.yml run --rm \
  warcraft-quest-immersion wqi corpus extract \
  --source-dump /app/data/sources/azerothcore/3.3.5/enUS/world.sql.gz \
  --source-artifact /app/data/sources/azerothcore/3.3.5/enUS/dbc.sql.gz \
  --source-version "AzerothCore DB version or commit" \
  --output /app/data/sources/azerothcore/3.3.5/enUS/corpus/wqi-3.3.5-enUS.zip
```

`manifest.json` records the world dump SHA-256, every supplied DBC artifact
hash, the supplied database version, available `version_db_world` rows,
extractor and schema versions, extraction timestamp, table row counts, schema
columns, content fingerprints, artifact hashes, locale, and reconciliation
counts.

The ZIP contract is:

- `entities.csv`: expansion-scoped creature, game-object, and item identities,
  inferred context, and inference evidence;
- `texts.csv`: stable logical quest/gossip identities and immutable source text;
- `bindings.csv`: one production unit per text and delivery endpoint;
- `triggers.csv`: quest relations and every reachable gossip-menu path;
- `quarantine.csv`: retained unresolved records and their explicit reasons.

Quest-log objectives and gossip option text are retained only in context. They
are never classified as NPC speech. Scripted `creature_text`, combat speech,
yells, whispers, and emote-only rows are outside this milestone.

## Validate, dry-run, and import

```bash
wqi corpus validate /path/to/wqi-3.3.5-enUS.zip
wqi corpus import /path/to/wqi-3.3.5-enUS.zip --dry-run
wqi corpus import /path/to/wqi-3.3.5-enUS.zip
```

The final command requires typing `IMPORT`; automation may use `--yes`. The
portal exposes the same workflow at `/alpha/import-export` and through:

```text
POST /api/alpha/corpus/validate
POST /api/alpha/corpus/import
```

An apply performs a production SQLite integrity check, makes and verifies a
timestamped backup, runs the import against a cloned database, and only
then opens an immediate transaction against production. A validation or apply
failure leaves the active snapshot unchanged. The dry-run reports added,
changed, removed, quarantined, source-changed, ambiguous-model, and addon
filename-conflict counts.

Source edits add immutable `dialogue_content_versions` rows. Existing spoken
revisions and audio remain stored, but affected bindings become
`source_changed`; they are excluded from production export until the shared
spoken text is reviewed and saved again. Manual NPC overrides and all Voice ID,
preset, sample, candidate, generation, and production-asset rows are preserved.

## Addon contract

New data modules may add these version-1-compatible tables:

```lua
QuestAudioLookupByNPCID[questID][stage][npcID]
QuestAudioLookupByObjectID[questID][stage][objectID]
QuestAudioLookupByItemID[questID][stage][itemID]
```

Quest filenames use the delivery endpoint, for example
`123-accept-c456.mp3`. The addon resolves the interacting GUID first, falls back
to the unique quest-log relation for clients that cannot expose an ID, and then
falls back to `123-accept.mp3` when the loaded data pack has no additive lookup.
The quest-progress event path is enabled for all supported legacy clients.

Phase 2 stops after the complete corpus is visible and reconciled and one
per-NPC binding passes in-game playback. Corpus extraction and import never call
ElevenLabs.
