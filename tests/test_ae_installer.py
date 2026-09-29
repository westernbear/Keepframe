from pathlib import Path

import pytest

from keepframe.after_effects import installer


def _panel_source(tmp_path: Path) -> Path:
    source = tmp_path / "keepframe_panel.jsx"
    source.write_text("// Keepframe panel\n", encoding="utf-8")
    return source


def _ae_installation(root: Path, year: int) -> Path:
    path = root / "Adobe" / f"Adobe After Effects {year}"
    support_files = path / "Support Files"
    support_files.mkdir(parents=True)
    (support_files / "AfterFX.exe").write_bytes(b"after-effects")
    return path


def test_install_refuses_non_windows(tmp_path):
    with pytest.raises(installer.NotWindowsError):
        installer.install_panel(
            platform="Linux",
            env={},
            panel_source=_panel_source(tmp_path),
        )


def test_discover_windows_installations_from_program_files(tmp_path):
    program_files = tmp_path / "Program Files"
    old = _ae_installation(program_files, 2021)
    current = _ae_installation(program_files, 2022)

    found = installer.discover_installations(
        platform="Windows",
        env={"ProgramFiles": str(program_files)},
    )

    assert found == (current,)
    assert old not in found


def test_discovery_requires_a_regular_afterfx_executable(tmp_path):
    program_files = tmp_path / "Program Files"
    selected = _ae_installation(program_files, 2024)
    (selected / "Support Files" / "AfterFX.exe").unlink()

    assert installer.discover_installations(
        platform="Windows",
        env={"ProgramFiles": str(program_files)},
    ) == ()


def test_install_refuses_ambiguous_discovery_with_choices(tmp_path):
    program_files = tmp_path / "Program Files"
    first = _ae_installation(program_files, 2022)
    second = _ae_installation(program_files, 2024)

    with pytest.raises(installer.MultipleInstallationsError) as error:
        installer.install_panel(
            platform="Windows",
            env={"ProgramFiles": str(program_files)},
            panel_source=_panel_source(tmp_path),
        )

    assert error.value.installations == (first, second)
    assert "--ae-path" in str(error.value)
    assert str(first) in str(error.value)
    assert str(second) in str(error.value)


def test_explicit_installation_selection_copies_panel(tmp_path):
    program_files = tmp_path / "Program Files"
    selected = _ae_installation(program_files, 2024)
    _ae_installation(program_files, 2022)
    source = _panel_source(tmp_path)

    panel_path = installer.install_panel(
        ae_path=selected,
        platform="Windows",
        env={"ProgramFiles": str(program_files)},
        panel_source=source,
    )

    destination = selected / "Support Files" / "Scripts" / "ScriptUI Panels" / "keepframe_panel.jsx"
    assert panel_path == destination
    assert destination.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


def test_install_rejects_symlinked_panel_destination(tmp_path):
    selected = _ae_installation(tmp_path / "Program Files", 2024)
    target = tmp_path / "outside"
    target.mkdir()
    scripts = selected / "Support Files" / "Scripts"
    scripts.mkdir()
    (scripts / "ScriptUI Panels").symlink_to(target, target_is_directory=True)

    with pytest.raises(installer.UnsafePathError):
        installer.install_panel(
            ae_path=selected,
            platform="Windows",
            env={},
            panel_source=_panel_source(tmp_path),
        )


def test_install_result_contains_manual_preferences_and_panel_instruction(tmp_path):
    selected = _ae_installation(tmp_path / "Program Files", 2024)
    installer.install_panel(
        ae_path=selected,
        platform="Windows",
        env={},
        panel_source=_panel_source(tmp_path),
    )

    instructions = installer.manual_instructions()
    assert instructions == installer.MANUAL_INSTRUCTIONS
    assert "Restart After Effects after installation." in instructions
    assert "Edit > Preferences > Scripting & Expressions" in instructions
    assert "Allow Scripts to Write Files and Access Network" in instructions
    assert "Window > Keepframe Panel." in instructions
