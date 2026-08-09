# Warcraft Quest Immersion

Warcraft Quest Immersion is a maintainable local workspace around the VoiceOver addon for World of Warcraft. It contains:

- the in-game `AI_VoiceOver` addon;
- the `AI_VoiceOverData_Vanilla` data module;
- a Python CLI for dialogue extraction, lookup generation, and ElevenLabs audio generation;
- a web control panel for CSV validation and lookup generation, with optional Basic Auth;
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

User-supplied archives belong in `imports/source-archives/`. The archive files
are ignored; their names, sizes, SHA-256 hashes, and inventory facts are tracked
in `imports/source-archives/manifest.json`.

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

Phase 2 artifacts live in `voice_profiles/`. The registry contains 46 complete
legacy race/gender profiles, five controlled delivery presets, locked comparison
scripts, and a 230-row placeholder preview matrix. Rebuild and validate the
generated CSV and manifest artifacts with:

```powershell
.\.venv\Scripts\python.exe scripts\build_baseline_voice_profiles.py
```

The web application now opens at `/alpha`. Its persistent production database
provides a filterable queue for every imported quest and gossip record, NPC
context, baseline-or-unique voice assignment, on-demand spoken-text revisions,
versioned voices and reference clips, guarded ElevenLabs actions, delivery
presets, audio review, and production approval.

The bundled CSV contains only four safe demonstration rows. Upload complete
validated exports through Alpha to populate the full corpus. Sources for
1.12.1, 2.4.3, 3.3.5, and current Classic coexist in one production database;
replacing one expansion/locale does not deactivate the others. Joined source
fields are retained as record metadata. Importing never creates spoken text or
contacts ElevenLabs. The uploaded sources, SQLite database, reference clips,
voice previews, and generated audio remain outside Git under `data/`.

## Container layout

| Container | Purpose | Published port | Required |
| --- | --- | --- | --- |
| `warcraft-quest-immersion` | Web control panel and all Python tooling | Host `8090` → container `8080` | Yes |
| `warcraft-quest-db` | MySQL 8.4 for rebuilding the full vMaNGOS dataset | None; private Compose network only | No |

The normal CSV workflow uses only `warcraft-quest-immersion`. Do not publish the MySQL port through a reverse proxy.

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

1. Construct a joined dialogue source from the AzerothCore quest, gossip, NPC,
   faction, and area data, then validate it through **Import / Export**.
2. Filter the complete queue by expansion, status, content type, race, gender,
   NPC, quest, or text.
3. Prepare spoken text on demand, then review or edit the revision.
4. Review the inferred NPC role, affiliation, zone, story reach, and concise
   context; assign a baseline or versioned unique voice. Returning an NPC to
   baseline retires its unused unique profile without deleting its history.
5. Create the reusable provider voice with description-only Voice Design,
   reference-guided Voice Design, or Instant Voice Cloning.
6. Explicitly request one ElevenLabs candidate, review it, and approve the exact
   file for production.
7. Treat production export as a later handoff after the corpus and approval
   rules have been validated.

All provider operations have a separate paid-action confirmation. If
ElevenLabs is not configured, they remain disabled and no request can be sent.
On the homelab, configure a newly created key without placing it in PowerShell
history or Git by running `scripts\configure-elevenlabs.cmd`. It prompts with
masked input, sends the value over SSH standard input, updates only the ignored
server `.env`, and recreates only `warcraft-quest-immersion`. Alpha's Settings
page then verifies the key and account usage through a read-only provider call.

ElevenLabs speech is metered by characters rather than LLM tokens. The alpha
uses the current published Multilingual v2/v3 list rate of $0.10 per 1,000
characters for preflight context and roughly one minute per 1,000 characters
for output-duration planning. These are estimates: the confirmation shows the
exact input size before each request, the API's `character-cost` response header
is recorded after speech generation, and the Settings page shows the account's
reported usage and limit. Voice Design returns three previews while charging its
preview text once; saving a chosen preview consumes a provider voice slot.

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

The current optional MySQL query path reflects the inherited vMaNGOS source and
is not the intended final 3.3.5 source of truth. The Phase 2 target is a
dedicated read-only AzerothCore extractor that emits the documented CSV transfer
contract with joined identifiers and NPC context fields.

Start the optional database container only when the full vMaNGOS source needs to be rebuilt:

```powershell
docker compose --profile warcraft-data up -d warcraft-quest-db
docker compose run --rm warcraft-quest-immersion wqi init-db
docker compose run --rm warcraft-quest-immersion wqi export-data --output /app/data/dialogue.csv
docker compose exec warcraft-quest-immersion wqi generate-lookups --input-csv /app/data/dialogue.csv
```

The database import is large and can take considerable time. Its named volume, `warcraft-quest-db-data`, persists independently of container replacement.

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
