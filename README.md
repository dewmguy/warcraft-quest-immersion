# Warcraft Quest Immersion

Warcraft Quest Immersion is a maintainable local workspace around the VoiceOver addon for World of Warcraft. It contains:

- the in-game `AI_VoiceOver` addon;
- the `AI_VoiceOverData_Vanilla` data module;
- a Python CLI for dialogue extraction, lookup generation, and ElevenLabs audio generation;
- an authenticated web control panel for CSV validation and lookup generation;
- a single-container default deployment, with MySQL available as an optional profile for full vMaNGOS data rebuilds.

The addon began with the open-source [WoW VoiceOver project](https://github.com/mrthinger/wow-voiceover). See [LICENSE](LICENSE) for licensing details.

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

2. Replace both `CHANGE_ME` values in `.env` with long unique passwords. `ELEVENLABS_API_KEY` can remain blank until audio generation is needed.

3. Build and start the single application container.

   ```powershell
   docker compose up -d --build warcraft-quest-immersion
   ```

4. Open `http://localhost:8090` and sign in with `WQI_ADMIN_USER` and `WQI_ADMIN_PASSWORD`.

The first start copies a four-row sample dialogue file into `data/dialogue.csv`. That sample is sufficient to validate the complete CSV-to-Lua lookup workflow without MySQL or an ElevenLabs account.

Verify the deployment:

```powershell
docker compose ps
Invoke-RestMethod http://localhost:8090/health
```

## Web workflow

The control panel supports two deliberately bounded operations:

1. Upload and validate a dialogue CSV.
2. Generate and download the addon's Lua lookup tables.

Database initialization and ElevenLabs audio generation remain CLI-only. This prevents a public web route from starting a long database import or a paid audio job.

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
