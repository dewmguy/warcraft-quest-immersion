from pathlib import Path

from tts_cli.paths import resolve_project_root


def test_project_root_honors_explicit_environment(tmp_path: Path):
    configured = tmp_path / "configured"

    assert resolve_project_root(environ={"WQI_PROJECT_ROOT": str(configured)}) == (
        configured.resolve()
    )


def test_project_root_uses_runtime_working_tree_for_installed_console_script(tmp_path: Path):
    working_root = tmp_path / "app"
    profile_dir = working_root / "voice_profiles"
    profile_dir.mkdir(parents=True)
    (profile_dir / "phase2-manifest.json").write_text("{}", encoding="utf-8")
    installed_module = tmp_path / "site-packages" / "tts_cli" / "paths.py"

    assert (
        resolve_project_root(
            module_file=installed_module,
            cwd=working_root,
            environ={},
        )
        == working_root.resolve()
    )
