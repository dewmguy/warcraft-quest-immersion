from tts_cli.npc_identity import infer_creature_identity


def test_model_path_resolves_race_when_display_extra_is_absent():
    identity = infer_creature_identity(
        name="Billy Maclure",
        creature_type=7,
        race_candidates=[],
        gender_candidates=[],
        model_paths=[r"Creature\HumanMaleKid\HumanMaleKid.mdx"],
        model_genders=[0],
    )

    assert identity.race_id == 1
    assert identity.gender_id == 0
    assert identity.race_basis == "CreatureModelData.ModelPath"
    assert identity.gender_basis == "creature_model_info.Gender"
    assert identity.ambiguous is False


def test_creature_type_replaces_narrator_for_non_character_models():
    identity = infer_creature_identity(
        name="Chromie",
        creature_type=2,
        race_candidates=[],
        gender_candidates=[],
        model_paths=[r"Creature\Dragon\NorthrendDragon.mdx"],
        model_genders=[1],
    )

    assert identity.race_id == -101
    assert identity.gender_id == 1
    assert identity.race_basis == "creature_template.type"


def test_unsexed_model_is_explicit_instead_of_defaulting_to_male():
    identity = infer_creature_identity(
        name="A'dal",
        creature_type=10,
        race_candidates=[],
        gender_candidates=[],
        model_paths=[r"Creature\Naaru\Naaru.mdx"],
        model_genders=[2],
    )

    assert identity.race_id == -118
    assert identity.gender_id == 2
    assert identity.gender_basis == "model gender unspecified"


def test_npc_title_can_resolve_unisex_model_gender():
    identity = infer_creature_identity(
        name="Princess Stillpine",
        creature_type=7,
        race_candidates=[],
        gender_candidates=[],
        model_paths=[r"Creature\Furbolg\Furbolg.mdx"],
        model_genders=[2],
    )

    assert identity.race_id == -108
    assert identity.gender_id == 1
    assert identity.gender_basis == "NPC title"


def test_type_not_specified_keeps_deliberate_narrator_fallback():
    identity = infer_creature_identity(
        name="Alterac Valley Portal",
        creature_type=10,
        race_candidates=[],
        gender_candidates=[],
        model_paths=[r"World\Generic\ActiveDoodads\SpellPortals\Portal.mdx"],
        model_genders=[2],
    )

    assert identity.race_id == -1
    assert identity.race_basis == "explicit narrator fallback"
