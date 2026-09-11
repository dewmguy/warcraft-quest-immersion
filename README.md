# Warcraft Quest Immersion

Warcraft Quest Immersion is a maintainable local workspace around the VoiceOver addon for World of Warcraft. It contains:

- the in-game `AI_VoiceOver` addon;
- the `AI_VoiceOverData_Vanilla` data module;
- a Python CLI for dialogue extraction, lookup generation, and ElevenLabs audio generation;
- a web production portal for corpus validation, review, and addon export, with optional Basic Auth;
- a single-container default deployment, with MySQL available as an optional profile for full vMaNGOS data rebuilds.

The addon began with the open-source [WoW VoiceOver project](https://github.com/mrthinger/wow-voiceover). See [LICENSE](LICENSE) for licensing details.

Development follows the explicitly approved gates in
[`docs/PHASES.md`](docs/PHASES.md). Work does not advance to the next production
phase until the current phase has been reviewed and approved.

The owner's noncommercial, transformative-use position and the project's
provenance requirements are recorded in
[`docs/CONTENT-POLICY.md`](docs/CONTENT-POLICY.md).

## Legacy audio and Git policy

Imported and newly generated dialogue audio is not stored in Git. Git contains
the application, addon source, provenance manifests, lookup metadata,
pronunciation dictionary, tests, and deployment tooling. Audio remains in
ignored persistent storage and is reattached by a verified import step.

Current inherited VoiceOver addon data archives belong in the ignored
`datapacks/` directory. They are mounted read-only at `/app/datapacks`; neither
the archives nor extracted audio are copied into the image or committed to Git.
The importer records each archive's SHA-256, module name, version, declared load
priority, asset path, duration, and matching corpus bindings in SQLite.

Inspect all packs and report their 3.3.5 enUS coverage without extracting audio:

```powershell
.\.venv\Scripts\python.exe -m tts_cli datapacks import .\datapacks --dry-run
```

Apply the verified import locally:

```powershell
.\.venv\Scripts\python.exe -m tts_cli datapacks import .\datapacks --yes
```

In Docker, use `/app/datapacks` as the directory. Identical MP3 payloads are
stored once by SHA-256 beneath `data/alpha/preproduced/assets/`. Every matching
quest or gossip binding receives a separately selectable candidate. The pack
with the highest declared data-module priority is selected by default only when
the line has no existing selection. Existing ElevenLabs selections are never
overwritten. Required `f-` and `m-` player-text files are retained together as
one publishable candidate with two playable assets.
Quest assets reconcile by quest ID and stage. Gossip assets reconcile through
the inherited lookup tables' creature/object ID or name plus source text; their
legacy MP3 hashes are provenance keys and are not assumed to equal the current
per-deliverer addon filename.

The older single-archive audit scripts remain available for historical
comparison. Archives used by that workflow belong in `imports/source-archives/`.

Verify, inventory, import, and validate a supplied legacy pack:

```powershell
.\.venv\Scripts\python.exe scripts\analyze_legacy_pack.py --verify-crc
.\.venv\Scripts\python.exe scripts\import_legacy_pack.py --verify-only
.\.venv\Scripts\python.exe scripts\import_legacy_pack.py
.\.venv\Scripts\python.exe scripts\validate_imported_audio.py
```

The default local import target is
`data/imported/mrthinger-vanilla-v1.0.0`, which is available inside the
application container through the existing `/app/data` volume.

## NPC cultural-reference research

Reviewed 3.3.5 reference findings live in
`assets/npc-references/3.3.5-enUS.json`. The catalog uses exact NPC names,
versioned source links, concise paraphrases, and optional performance clues.
Weak or incidental matches are omitted. Importing it creates additive reference
records without changing manually edited character context:

```powershell
.\.venv\Scripts\python.exe -m tts_cli references import --dry-run
.\.venv\Scripts\python.exe -m tts_cli references import --yes
```

Matches appear in the Character Context area of the NPC and unique-voice
profiles. NPC search also includes the reference title and summary. A catalog
can set `matched_voice_scope` to `unique` to activate a unique profile for every
matched database record. New unique voice profiles receive the researched
context, while existing versioned voice prompts are not silently rewritten.
NPC profiles also list any other records in the same expansion and entity type
that use the exact same name, including their dialogue counts and voice status.

## Pronunciation dictionary

The owner-reviewed source is `pronunciation/warcraft-en-US.csv`. Build the
ElevenLabs IPA and alias dictionaries with:

```powershell
.\.venv\Scripts\python.exe scripts\build_pronunciation_dictionary.py
```

All retained entries passed the first owner review. The generated JSON still
separates phoneme and alias forms so the correct form can be selected for the
chosen ElevenLabs model.

## Voice baseline registry

Phase 2 artifacts live in `voice_profiles/`. The registry contains 98 complete
legacy and model-derived race/gender profiles, five controlled delivery presets,
locked comparison scripts, and a 490-row placeholder preview matrix. Rebuild and validate the
generated CSV and manifest artifacts with:

```powershell
.\.venv\Scripts\python.exe scripts\build_baseline_voice_profiles.py
```

The web application now opens at `/alpha`. Its persistent production database
provides separate filterable queues for quests, NPC gossip, and readable object
pages, plus NPC context, baseline-or-unique voice assignment, deterministically prepared quest
spoken-text revisions,
versioned voices and reference clips, guarded ElevenLabs actions, delivery
presets, audio review, and production approval.

The bundled CSV contains only four safe demonstration rows. Upload complete
validated exports through Alpha to populate the full corpus. Sources for
1.12.1, 2.4.3, 3.3.5, and current Classic coexist in one production database;
replacing one expansion/locale does not deactivate the others. Joined source
fields are retained as record metadata. Importing creates missing deterministic
spoken-text revisions for quests without overwriting reviewed revisions or
contacting ElevenLabs. Gossip spoken text remains explicitly prepared; readable
object text is prepared when its profile is first opened. The
uploaded sources, SQLite database, reference clips,
voice previews, and generated audio remain outside Git under `data/`.

The authoritative 3.3.5 enUS workflow uses a certified, versioned corpus ZIP,
not the flat demonstration CSV. Extraction, reconciliation, atomic import,
source-change handling, and the additive per-NPC addon contract are documented
in [`docs/CORPUS.md`](docs/CORPUS.md).

## Container layout

| Container | Purpose | Published port | Required |
| --- | --- | --- | --- |
| `warcraft-quest-immersion` | Web control panel and all Python tooling | Host `8090` → container `8080` | Yes |
| `warcraft-quest-db` | MySQL 8.4 for rebuilding the full vMaNGOS dataset | None; private Compose network only | No |
| `warcraft-quest-source-db` | On-demand MariaDB restore of the authoritative AzerothCore snapshot | None; private Compose network only | No |

Normal portal operation uses only `warcraft-quest-immersion`. Do not publish either database through a reverse proxy.

## Quick start with Docker

1. Copy the environment template.

   ```powershell
   Copy-Item .env.example .env
   ```

2. Replace the MySQL `CHANGE_ME` values in `.env` with a long unique password. `ELEVENLABS_API_KEY` can remain blank until audio generation is needed. Leave `WQI_ADMIN_PASSWORD` blank when Pangolin SSO protects the route, or set it to enable application-level Basic Auth.

3. Build and start the single application container.

   ```powershell
   docker compose up -d --build warcraft-quest-immersion
   ```

4. Open `http://localhost:8090`. If `WQI_ADMIN_PASSWORD` is set, sign in with `WQI_ADMIN_USER` and that password.

The first start copies a four-row sample dialogue file into `data/dialogue.csv`. That sample is sufficient to validate the complete CSV-to-Lua lookup workflow without MySQL or an ElevenLabs account.

Verify the deployment:

```powershell
docker compose ps
Invoke-RestMethod http://localhost:8090/health
```

## Web workflow

The Alpha portal is the working production surface:

1. Extract a certified corpus bundle from the restored AzerothCore world
   snapshot, validate its reconciliation report, then explicitly apply it
   through **Import / Export**.
2. Filter the complete queue by expansion, status, content type, race, gender,
   NPC, quest, or text.
3. Review the automatically prepared quest and readable-object spoken text and
   edit it only when needed. Prepare gossip spoken text explicitly.
4. Review the inferred NPC role, affiliation, zone, story reach, and concise
   context; assign a baseline or versioned unique voice. Returning an NPC to
   baseline retires its unused unique profile without deleting its history.
5. Create the reusable provider voice with description-only Voice Design,
   reference-guided Voice Design, or Instant Voice Cloning.
   Reference uploads accept batches of MP3, WAV, M4A, OGG, or FLAC files. The
   original files are preserved byte-for-byte outside Git and can be reviewed
   from compact, collapsible rows or deleted individually before provider submission.
6. Explicitly request one ElevenLabs candidate, review it, and approve the exact
   file for production.
7. Treat production export as a later handoff after the corpus and approval
   rules have been validated.

Provider generation actions begin when their button is clicked, while deletion
and approval actions retain confirmation where appropriate. If ElevenLabs is
not configured, provider actions remain disabled and no request can be sent.
On the homelab, configure a newly created key without placing it in PowerShell
history or Git by running `scripts\configure-elevenlabs.cmd`. It prompts with
masked input, sends the value over SSH standard input, updates only the ignored
server `.env`, and recreates only `warcraft-quest-immersion`. Alpha's Settings
page then verifies the key and account usage through a read-only provider call.

ElevenLabs calls its subscription units credits, although the API still uses
legacy `character_count`, `character_limit`, and `character-cost` field names.
The sticky header shows the provider-reported credits used and available for the
current billing period, then refreshes after every generation request. Preflight
cards show input size, estimated credits, and roughly one minute of audio per
1,000 characters; the provider-reported credit cost is recorded after generation.
The alpha does not estimate cash or overage charges. Voice Design returns three
previews while charging its preview text once; saving a chosen preview consumes
a provider voice slot.

For a public hostname, enable Pangolin SSO or set `WQI_ADMIN_PASSWORD`. When both are disabled, the control panel is intentionally open.

The expected CSV columns are:

| Column | Meaning |
| --- | --- |
| `source` | `accept`, `progress`, `complete`, or `gossip` |
| `quest` | Quest ID, or empty for gossip |
| `quest_title` | Localized quest title |
| `text` | Dialogue text for the selected locale |
| `DisplayRaceID` | NPC race ID; `-1` means narrator/inanimate object |
| `DisplaySexID` | `0` for male, `1` for female |
| `name` | Creature, object, or item name |
| `type` | `creature`, `gameobject`, or `item` |
| `id` | Creature, object, or item ID |
| `original_text` | Original template text used for stable audio hashes |

## Full database workflow

The inherited vMaNGOS database remains available only for legacy comparison.
Start it only when that older dataset needs to be rebuilt:

```powershell
docker compose --profile warcraft-data up -d warcraft-quest-db
docker compose run --rm warcraft-quest-immersion wqi init-db
docker compose run --rm warcraft-quest-immersion wqi export-data --output /app/data/dialogue.csv
docker compose exec warcraft-quest-immersion wqi generate-lookups --input-csv /app/data/dialogue.csv
```

The database import is large and can take considerable time. Its named volume, `warcraft-quest-db-data`, persists independently of container replacement.

For the authoritative AzerothCore workflow, stage the private dump and matching
DBC artifacts under `data/sources/azerothcore/3.3.5/enUS/`. Raw build-12340 DBCs
can be extracted and converted with `scripts/extract-335a-dbc.ps1` and
`wqi corpus dbc-to-sql`. Configure the
`AZEROTHCORE_MYSQL_*` values in `.env`, then follow
[`docs/CORPUS.md`](docs/CORPUS.md). The `corpus-build` service has no published
port and is started only for restore and extraction.

Supported locale codes are `enUS`, `enGB`, `koKR`, `frFR`, `deDE`, `zhCN`, `zhTW`, `esES`, `esMX`, and `ruRU`.

## Audio generation

Add `ELEVENLABS_API_KEY` to `.env`, then use the interactive CLI with a CSV export:

```powershell
docker compose run --rm warcraft-quest-immersion wqi interactive --input-csv /app/data/dialogue.csv
```

ElevenLabs voices are expected to use `race-gender` names such as `orc-male`. The supported race mapping is maintained in `tts_cli/consts.py`. Generated MP3 files and lookup tables are written beneath `AI_VoiceOverData_Vanilla/generated`.

## Native Python development

Python 3.10 or 3.11 is supported. On Windows PowerShell:

```powershell
.\scripts\bootstrap.ps1
.\scripts\check.ps1
.\.venv\Scripts\python.exe -m tts_cli doctor
.\.venv\Scripts\python.exe -m tts_cli generate-lookups --input-csv assets\samples\dialogue.csv
```

The legacy `python cli-main.py ...` launcher remains available, but `python -m tts_cli ...` and the installed `wqi` command are preferred.

## Homelab deployment

The checked-in server Compose fragment is [deploy/compose-services.yml](deploy/compose-services.yml). On the current homelab it is merged into `/home/plex/docker-compose.yml`, while the checkout and persistent files live at `/opt/warcraft-quest-immersion`.

The only Pangolin target is:

```text
Resource: warcraftproject.wabsite.tech
Target:   http://172.16.1.2:8090
Container: warcraft-quest-immersion
```

After an approved commit reaches GitHub, the server update path is:

```bash
/opt/warcraft-quest-immersion/scripts/deploy-server.sh
```

That script refuses a dirty checkout, pulls with `--ff-only`, validates the complete shared stack, rebuilds only `warcraft-quest-immersion`, replaces only that service, and waits for its health check. It does not recreate Plex or unrelated containers.

## Project checks

Every push and pull request runs:

- Ruff lint and format verification;
- the Python test suite;
- the dependency-free local doctor check;
- a complete Docker image build.

Run the same checks locally with `scripts/check.ps1`.
