import pytest, pathlib, tempfile

@pytest.fixture
def tmp_scene_dir():
    d = tempfile.mkdtemp(prefix="keepframe-")
    return pathlib.Path(d)
