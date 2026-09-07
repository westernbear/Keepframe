import json
from pathlib import Path
from keepframe.web.workspace import list_projects, create_project, project_dir

def test_empty_workspace(tmp_path):
    assert list_projects(tmp_path) == []

def test_create_requires_video(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        create_project(tmp_path, "Autumn", tmp_path / "nope.mp4", "range", (0, 30))
