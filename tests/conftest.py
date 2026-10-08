
import pytest, pathlib, tempfile


@pytest.fixture
def tmp_scene_dir():
    d = tempfile.mkdtemp(prefix="keepframe-")
    return pathlib.Path(d)


@pytest.fixture(autouse=True, scope="session")
def _font_subset_cache(tmp_path_factory):
    """Font subsets go to a session tmp dir, never ~/.cache."""
    import os
    old = os.environ.get("KEEPFRAME_FONT_CACHE")
    os.environ["KEEPFRAME_FONT_CACHE"] = str(tmp_path_factory.mktemp("font-subsets"))
    yield
    if old is None:
        os.environ.pop("KEEPFRAME_FONT_CACHE", None)
    else:
        os.environ["KEEPFRAME_FONT_CACHE"] = old
