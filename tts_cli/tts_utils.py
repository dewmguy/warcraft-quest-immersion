import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
from slpp import slpp as lua
from tqdm import tqdm

from tts_cli.config import Settings, load_settings
from tts_cli.consts import GENDER_DICT, RACE_DICT
from tts_cli.length_table import write_sound_length_table_lua
from tts_cli.paths import MODULE_NAME, OUTPUT_DIR, SOUND_OUTPUT_DIR
from tts_cli.utils import get_first_n_words, get_last_n_words, replace_dollar_bs_with_space

OUTPUT_FOLDER = str(OUTPUT_DIR)
SOUND_OUTPUT_FOLDER = str(SOUND_OUTPUT_DIR)
DATAMODULE_TABLE_GUARD_CLAUSE = "if not VoiceOver or not VoiceOver.DataModules then return end"
REPLACE_DICT = {
    "$b": "\n",
    "$B": "\n",
    "$n": "adventurer",
    "$N": "Adventurer",
    "$C": "Adventurer",
    "$c": "adventurer",
    "$R": "Traveler",
    "$r": "traveler",
}


def get_hash(text):
    hash_object = hashlib.md5(text.encode())
    return hash_object.hexdigest()


def create_output_subdirs(subdir: str):
    output_subdir = os.path.join(SOUND_OUTPUT_FOLDER, subdir)
    if not os.path.exists(output_subdir):
        os.makedirs(output_subdir)


def _per_entity_audio_ready(row) -> bool:
    value = row.get("per_entity_audio_ready", True)
    if pd.isna(value):
        return False
    return str(value).strip().lower() not in {"", "0", "false", "no"}


def prune_quest_id_table(quest_id_table):
    def is_single_quest_id(nested_dict):
        if isinstance(nested_dict, dict):
            if len(nested_dict) == 1:
                return is_single_quest_id(next(iter(nested_dict.values())))
            else:
                return False
        else:
            return True

    def single_quest_id(nested_dict):
        if isinstance(nested_dict, dict):
            return single_quest_id(next(iter(nested_dict.values())))
        else:
            return nested_dict

    pruned_table = {}
    for source_key, source_value in quest_id_table.items():
        pruned_table[source_key] = {}
        for title_key, title_value in source_value.items():
            if is_single_quest_id(title_value):
                pruned_table[source_key][title_key] = single_quest_id(title_value)
            else:
                pruned_table[source_key][title_key] = {}
                for npc_key, npc_value in title_value.items():
                    if is_single_quest_id(npc_value):
                        pruned_table[source_key][title_key][npc_key] = single_quest_id(npc_value)
                    else:
                        pruned_table[source_key][title_key][npc_key] = npc_value

    return pruned_table


class TTSProcessor:
    def __init__(
        self,
        *,
        fetch_voices: bool = True,
        settings: Settings | None = None,
        session: requests.Session | None = None,
    ):
        self.settings = settings or load_settings()
        self.session = session or requests.Session()
        self.voice_map = self.fetch_voice_map() if fetch_voices else {}

    def get_voice_map(self):
        return self.voice_map

    def fetch_voice_map(self):
        url = "https://api.elevenlabs.io/v1/voices"
        headers = {"xi-api-key": self.settings.require_elevenlabs()}

        try:
            response = self.session.get(url, headers=headers, timeout=30)
            response.raise_for_status()
        except requests.RequestException as error:
            raise RuntimeError(f"Could not fetch ElevenLabs voices: {error}") from error

        response = response.json()
        voice_map = {}

        for voice in response["voices"]:
            name_parts = voice["name"].split("-")
            if len(name_parts) == 2:
                race, gender = name_parts
                if race in RACE_DICT.values() and (gender == "male" or gender == "female"):
                    voice_map[voice["name"]] = voice["voice_id"]

        return voice_map

    def tts(
        self, text: str, voice: str, outputName: str, output_subfolder: str, forceGen: bool = False
    ):
        result = ""
        outpath = os.path.join(SOUND_OUTPUT_FOLDER, output_subfolder, outputName)
        if os.path.isfile(outpath) and forceGen is not True:
            result = "duplicate generation, skipping"
            return

        voice_id = self.voice_map[voice]
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        payload = {"text": text, "voice_settings": {"stability": 0.28, "similarity_boost": 0.992}}
        headers = {"xi-api-key": self.settings.require_elevenlabs()}

        try:
            response = self.session.post(url, json=payload, headers=headers, timeout=120)
        except requests.RequestException as error:
            result = f"Error generating {outpath}: {error}"
            print(result)
            return result

        if response.status_code == 200 and response.headers.get("Content-Type", "").startswith(
            "audio/mpeg"
        ):
            with open(outpath, "wb") as f:
                f.write(response.content)
                result = f"Audio file saved successfully!: {outpath}"
                print(result)
        else:
            result = f"Error: unable to save audio file {response}"
            print(result)
        return result

    def handle_gender_options(self, text):
        pattern = re.compile(r"\$[Gg]\s*([^:;]+?)\s*:\s*([^:;]+?)\s*;")

        male_text = pattern.sub(r"\1", text)
        female_text = pattern.sub(r"\2", text)

        return male_text, female_text

    def preprocess_dataframe(self, df):
        df = df.copy()  # prevent mutation on original df for safety
        df["race"] = df["DisplayRaceID"].map(RACE_DICT)
        df["gender"] = df["DisplaySexID"].map(GENDER_DICT)
        if df["race"].isna().any():
            unknown = sorted(df.loc[df["race"].isna(), "DisplayRaceID"].unique())
            raise ValueError(f"Unsupported NPC race IDs: {unknown}")
        if df["gender"].isna().any():
            unknown = sorted(df.loc[df["gender"].isna(), "DisplaySexID"].unique())
            raise ValueError(f"Unsupported NPC gender IDs: {unknown}")
        df["voice_name"] = df["race"] + "-" + df["gender"]

        df["templateText_race_gender"] = df["original_text"] + df["race"] + df["gender"]
        df["templateText_race_gender_hash"] = df["templateText_race_gender"].apply(get_hash)

        df["cleanedText"] = df["text"].copy()

        for k, v in REPLACE_DICT.items():
            df["cleanedText"] = df["cleanedText"].str.replace(k, v, regex=False)

        df["cleanedText"] = df["cleanedText"].str.replace(r"<.*?>\s", "", regex=True)

        df["player_gender"] = None
        rows = []
        for _, row in df.iterrows():
            if re.search(r"\$[Gg]", row["cleanedText"]):
                male_text, female_text = self.handle_gender_options(row["cleanedText"])

                row_male = row.copy()
                row_male["cleanedText"] = male_text
                row_male["player_gender"] = "m"

                row_female = row.copy()
                row_female["cleanedText"] = female_text
                row_female["player_gender"] = "f"

                rows.extend([row_male, row_female])
            else:
                rows.append(row)

        new_df = pd.DataFrame(rows)
        new_df.reset_index(drop=True, inplace=True)

        return new_df

    def process_row(self, row_tuple):
        row = pd.Series(row_tuple[1:], index=row_tuple._fields[1:])
        voice_name = f"{row['race']}-{row['gender']}"
        custom_message = ""
        if "$" in row["cleanedText"] or "<" in row["cleanedText"] or ">" in row["cleanedText"]:
            custom_message = f"skipping due to invalid chars: {row['cleanedText']}"
        elif voice_name not in self.selected_voice_names:
            custom_message = f"skipping due to voice being unselected or unavailable: {voice_name}"
        else:
            self.tts_row(row, voice_name)
        return custom_message

    def tts_row(self, row, voice_name):
        tts_text = row["cleanedText"]
        addon_file_key = row.get("addon_file_key", "")
        if pd.isna(addon_file_key):
            addon_file_key = ""
        file_name = str(addon_file_key).strip() or (
            f"{row['quest']}-{row['source']}"
            if row["quest"]
            else f"{row['templateText_race_gender_hash']}"
        )
        if row["player_gender"] is not None:
            file_name = row["player_gender"] + "-" + file_name
        file_name = file_name + ".mp3"
        subfolder = "quests" if row["quest"] else "gossip"
        self.tts(tts_text, voice_name, file_name, subfolder)

    def create_output_dirs(self):
        create_output_subdirs("")
        create_output_subdirs("quests")
        create_output_subdirs("gossip")

    def process_rows_in_parallel(
        self, df, row_proccesing_fn, selected_voice_names: list[str], max_workers=5
    ):

        total_rows = len(df)
        bar_format = (
            "{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}] {postfix}"
        )
        self.selected_voice_names = set(selected_voice_names)

        with (
            tqdm(
                total=total_rows,
                unit="rows",
                ncols=100,
                desc="Generating Audio",
                ascii=False,
                bar_format=bar_format,
                dynamic_ncols=True,
            ) as pbar,
            ThreadPoolExecutor(max_workers=max_workers) as executor,
        ):
            for _row, custom_message in zip(
                df.iterrows(), executor.map(row_proccesing_fn, df.itertuples()), strict=True
            ):
                pbar.set_postfix_str(custom_message)
                pbar.update(1)

    def write_gossip_file_lookups_table(self, df, module_name, type, table, filename):
        output_file = OUTPUT_FOLDER + f"/{filename}.lua"
        gossip_table = {}

        accept_df = df[(df["quest"] == "") & (df["type"] == type)]

        for _i, row in tqdm(accept_df.iterrows()):
            if row["id"] not in gossip_table:
                gossip_table[row["id"]] = {}

            escapedText = row["text"].replace('"', "'").replace("\r", " ").replace("\n", " ")
            addon_file_key = row.get("addon_file_key", "")
            if pd.isna(addon_file_key):
                addon_file_key = ""
            gossip_table[row["id"]][escapedText] = (
                str(addon_file_key).strip()
                if _per_entity_audio_ready(row) and str(addon_file_key).strip()
                else row["templateText_race_gender_hash"]
            )

        with open(output_file, "w", encoding="UTF-8") as f:
            f.write(DATAMODULE_TABLE_GUARD_CLAUSE + "\n")
            f.write(f"{module_name}.{table} = ")
            f.write(lua.encode(gossip_table))
            f.write("\n")

        print(f"Finished writing {filename}.lua")

    def write_questlog_npc_lookups_table(self, df, module_name, type, table, filename):
        output_file = OUTPUT_FOLDER + f"/{filename}.lua"
        questlog_table = {}
        audio_table = {}

        accept_df = df[(df["source"] == "accept") & (df["type"] == type)]
        for quest_id, rows in accept_df.groupby("quest"):
            entity_ids = sorted({int(value) for value in rows["id"]})
            if len(entity_ids) == 1:
                questlog_table[int(quest_id)] = entity_ids[0]

        quest_df = df[(df["quest"] != "") & (df["type"] == type)]
        for _i, row in tqdm(quest_df.iterrows()):
            if not _per_entity_audio_ready(row):
                continue
            quest_id = int(row["quest"])
            stage = str(row["source"])
            entity_id = int(row["id"])
            addon_file_key = row.get("addon_file_key", "")
            if pd.isna(addon_file_key):
                addon_file_key = ""
            filename_key = str(addon_file_key).strip() or f"{quest_id}-{stage}"
            audio_table.setdefault(quest_id, {}).setdefault(stage, {})[entity_id] = filename_key

        audio_table_name = {
            "creature": "QuestAudioLookupByNPCID",
            "gameobject": "QuestAudioLookupByObjectID",
            "item": "QuestAudioLookupByItemID",
        }[type]

        with open(output_file, "w", encoding="UTF-8") as f:
            f.write(DATAMODULE_TABLE_GUARD_CLAUSE + "\n")
            f.write(f"{module_name}.{table} = ")
            f.write(lua.encode(questlog_table))
            f.write("\n")
            f.write(f"{module_name}.{audio_table_name} = ")
            f.write(lua.encode(audio_table))
            f.write("\n")

        print(f"Finished writing {filename}.lua")

    def write_npc_name_lookup_table(self, df, module_name, type, table, filename):
        output_file = OUTPUT_FOLDER + f"/{filename}.lua"
        npc_name_table = {}

        accept_df = df[df["type"] == type]

        for _i, row in tqdm(accept_df.iterrows()):
            npc_name_table[row["id"]] = row["name"]

        with open(output_file, "w", encoding="UTF-8") as f:
            f.write(DATAMODULE_TABLE_GUARD_CLAUSE + "\n")
            f.write(f"{module_name}.{table} = ")
            f.write(lua.encode(npc_name_table))
            f.write("\n")

        print(f"Finished writing {filename}.lua")

    def write_quest_id_lookup(self, df, module_name):
        output_file = OUTPUT_FOLDER + "/quest_id_lookups.lua"
        quest_id_table = {}

        quest_df = df[df["quest"] != ""]

        for _i, row in tqdm(quest_df.iterrows()):
            quest_source = row["source"]
            quest_id = int(row["quest"])
            quest_title = row["quest_title"]
            quest_text = (
                get_first_n_words(row["text"], 15) + " " + get_last_n_words(row["text"], 15)
            )
            escaped_quest_text = replace_dollar_bs_with_space(
                quest_text.replace('"', "'").replace("\r", " ").replace("\n", " ")
            )
            escaped_quest_title = (
                quest_title.replace('"', "'").replace("\r", " ").replace("\n", " ")
            )
            npc_name = row["name"]
            escaped_npc_name = npc_name.replace('"', "'").replace("\r", " ").replace("\n", " ")

            # table[source][title][npcName][text]
            if quest_source not in quest_id_table:
                quest_id_table[quest_source] = {}

            if escaped_quest_title not in quest_id_table[quest_source]:
                quest_id_table[quest_source][escaped_quest_title] = {}

            if escaped_npc_name not in quest_id_table[quest_source][escaped_quest_title]:
                quest_id_table[quest_source][escaped_quest_title][escaped_npc_name] = {}

            if (
                escaped_quest_text
                not in quest_id_table[quest_source][escaped_quest_title][escaped_npc_name]
            ):
                quest_id_table[quest_source][escaped_quest_title][escaped_npc_name][
                    escaped_quest_text
                ] = quest_id

        pruned_quest_id_table = prune_quest_id_table(quest_id_table)

        # UTF-8 Encoding is important for other languages!
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(DATAMODULE_TABLE_GUARD_CLAUSE + "\n")
            f.write(f"{module_name}.QuestIDLookup = ")
            f.write(lua.encode(pruned_quest_id_table))
            f.write("\n")

    def write_npc_name_gossip_file_lookups_table(self, df, module_name, type, table, filename):
        output_file = OUTPUT_FOLDER + f"/{filename}.lua"
        gossip_table = {}

        accept_df = df[(df["quest"] == "") & (df["type"] == type)]

        for _i, row in tqdm(accept_df.iterrows()):
            npc_name = row["name"]
            escaped_npc_name = npc_name.replace('"', "'").replace("\r", " ").replace("\n", " ")

            if escaped_npc_name not in gossip_table:
                gossip_table[escaped_npc_name] = {}

            escapedText = row["text"].replace('"', "'").replace("\r", " ").replace("\n", " ")

            addon_file_key = row.get("addon_file_key", "")
            if pd.isna(addon_file_key):
                addon_file_key = ""
            gossip_table[escaped_npc_name][escapedText] = (
                str(addon_file_key).strip()
                if _per_entity_audio_ready(row) and str(addon_file_key).strip()
                else row["templateText_race_gender_hash"]
            )

        with open(output_file, "w", encoding="UTF-8") as f:
            f.write(DATAMODULE_TABLE_GUARD_CLAUSE + "\n")
            f.write(f"{module_name}.{table} = ")
            f.write(lua.encode(gossip_table))
            f.write("\n")

        print(f"Finished writing {filename}.lua")

    def tts_dataframe(self, df, selected_voices):
        self.create_output_dirs()
        self.process_rows_in_parallel(df, self.process_row, selected_voices, max_workers=5)
        print("Audio finished generating.")

    def generate_lookup_tables(self, df):
        self.create_output_dirs()
        self.write_gossip_file_lookups_table(
            df, MODULE_NAME, "creature", "GossipLookupByNPCID", "npc_gossip_file_lookups"
        )
        self.write_gossip_file_lookups_table(
            df, MODULE_NAME, "gameobject", "GossipLookupByObjectID", "object_gossip_file_lookups"
        )

        self.write_quest_id_lookup(df, MODULE_NAME)
        print("Finished writing quest_id_lookups.lua")

        self.write_npc_name_gossip_file_lookups_table(
            df, MODULE_NAME, "creature", "GossipLookupByNPCName", "npc_name_gossip_file_lookups"
        )
        self.write_npc_name_gossip_file_lookups_table(
            df,
            MODULE_NAME,
            "gameobject",
            "GossipLookupByObjectName",
            "object_name_gossip_file_lookups",
        )

        self.write_questlog_npc_lookups_table(
            df, MODULE_NAME, "creature", "NPCIDLookupByQuestID", "questlog_npc_lookups"
        )
        self.write_questlog_npc_lookups_table(
            df, MODULE_NAME, "gameobject", "ObjectIDLookupByQuestID", "questlog_object_lookups"
        )
        self.write_questlog_npc_lookups_table(
            df, MODULE_NAME, "item", "ItemIDLookupByQuestID", "questlog_item_lookups"
        )

        self.write_npc_name_lookup_table(
            df, MODULE_NAME, "creature", "NPCNameLookupByNPCID", "npc_name_lookups"
        )
        self.write_npc_name_lookup_table(
            df, MODULE_NAME, "gameobject", "ObjectNameLookupByObjectID", "object_name_lookups"
        )
        self.write_npc_name_lookup_table(
            df, MODULE_NAME, "item", "ItemNameLookupByItemID", "item_name_lookups"
        )

        write_sound_length_table_lua(MODULE_NAME, SOUND_OUTPUT_FOLDER, OUTPUT_FOLDER)
        print("Updated sound_length_table.lua")
