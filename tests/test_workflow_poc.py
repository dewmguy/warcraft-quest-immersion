import pytest

from tts_cli.voice_profiles import load_phase2_review
from tts_cli.workflow_poc import WorkflowError, WorkflowPoc


@pytest.fixture
def poc(tmp_path):
    workflow = WorkflowPoc(tmp_path / "dwarf-poc.sqlite3")
    workflow.initialize()
    return workflow


def _profile(bundle, profile_id):
    return next(item for item in bundle["profiles"] if item["profile"]["profile_id"] == profile_id)


def test_bundle_is_limited_to_two_dwarf_profiles_and_no_audio(poc):
    bundle = poc.bundle(load_phase2_review())

    assert bundle["no_audio_mode"] is True
    assert bundle["project_phase"]["name"] == "Voice Workbench"
    assert {item["profile"]["profile_id"] for item in bundle["profiles"]} == {
        "dwarf-male",
        "dwarf-female",
    }
    assert all(item["character_estimate"] > 0 for item in bundle["profiles"])


def test_settings_persist_and_neutral_cannot_be_disabled(poc):
    updated = poc.save_settings(
        "dwarf-male",
        {
            "source_strategy": "reference_design",
            "neutral_script": "A sufficiently long neutral comparison sentence for this Dwarf profile.",
            "enabled_deliveries": ["neutral", "angry"],
            "design_notes": "Keep the influence light.",
        },
    )

    assert updated["source_strategy"] == "reference_design"
    profile = _profile(poc.bundle(load_phase2_review()), "dwarf-male")
    assert profile["settings"]["enabled_deliveries"] == ["neutral", "angry"]

    with pytest.raises(WorkflowError, match="Neutral must remain enabled"):
        poc.save_settings(
            "dwarf-male",
            {
                "neutral_script": "A sufficiently long comparison sentence for validation.",
                "enabled_deliveries": ["angry"],
            },
        )


def test_profile_gates_advance_sequentially_and_preserve_history(poc):
    poc.transition(
        entity_type="profile",
        entity_id="dwarf-female",
        stage_id="identity_defined",
        action="approve",
        note="Scope accepted.",
    )
    profile = _profile(poc.bundle(load_phase2_review()), "dwarf-female")
    states = {stage["id"]: stage["status"] for stage in profile["profile_stages"]}

    assert states["identity_defined"] == "approved"
    assert states["source_strategy"] == "current"
    assert profile["history"][0]["note"] == "Scope accepted."

    with pytest.raises(WorkflowError, match="future stage"):
        poc.transition(
            entity_type="profile",
            entity_id="dwarf-female",
            stage_id="neutral_approved",
            action="request_changes",
        )


def test_reopening_a_gate_resets_later_status_but_keeps_events(poc):
    for stage_id in ["identity_defined", "source_strategy", "neutral_candidate"]:
        poc.transition(
            entity_type="profile",
            entity_id="dwarf-male",
            stage_id=stage_id,
            action="approve",
        )
    poc.transition(
        entity_type="profile",
        entity_id="dwarf-male",
        stage_id="source_strategy",
        action="reopen",
        note="Try a reference-assisted design instead.",
    )
    profile = _profile(poc.bundle(load_phase2_review()), "dwarf-male")
    states = {stage["id"]: stage["status"] for stage in profile["profile_stages"]}

    assert states["source_strategy"] == "current"
    assert states["neutral_candidate"] == "not_started"
    assert len(profile["history"]) == 4


def test_unique_line_stages_can_be_marked_not_required(poc):
    profile = _profile(poc.bundle(load_phase2_review()), "dwarf-male")
    line_id = profile["demo_line"]["line_id"]
    for stage_id in ["imported_checked", "text_processed", "short_baseline", "complete_baseline"]:
        poc.transition(
            entity_type="line",
            entity_id=line_id,
            stage_id=stage_id,
            action="approve",
        )
    poc.transition(
        entity_type="line",
        entity_id=line_id,
        stage_id="short_unique",
        action="skip_unique",
        note="Ordinary NPC; baseline is sufficient.",
    )
    profile = _profile(poc.bundle(load_phase2_review()), "dwarf-male")
    states = {stage["id"]: stage["status"] for stage in profile["line_stages"]}

    assert states["short_unique"] == "not_required"
    assert states["complete_unique"] == "current"
