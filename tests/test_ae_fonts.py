from __future__ import annotations

import os
import hashlib
import struct
from types import SimpleNamespace

import pytest

import keepframe.after_effects.fonts as fonts_module
from keepframe.after_effects.fonts import enrich_heartbeat_fonts
from keepframe.after_effects.models import AECapabilities


def _sfnt(*, postscript: str, family: str, style: str, version: str | None = None) -> bytes:
    values = [(6, postscript), (1, family), (2, style)]
    if version is not None:
        values.append((5, version))
    encoded = [value.encode("utf-16-be") for _, value in values]
    string_offset = 6 + 12 * len(values)
    records = bytearray()
    strings = bytearray()
    for (name_id, _), value in zip(values, encoded):
        records.extend(
            struct.pack(">HHHHHH", 3, 1, 0x0409, name_id, len(value), len(strings))
        )
        strings.extend(value)
    name_table = struct.pack(">HHH", 0, len(values), string_offset) + records + strings
    table_offset = 28
    header = struct.pack(">IHHHH", 0x00010000, 1, 0, 0, 0)
    directory = struct.pack(">4sIII", b"name", 0, table_offset, len(name_table))
    return header + directory + name_table


def _heartbeat(fonts: list[dict[str, object]], names: list[str]) -> dict[str, object]:
    return {
        "version": "24.1.0",
        "major": 24,
        "host": "after-effects",
        "ready": True,
        "project_open": True,
        "timestamp": 1.0,
        "capabilities": {
            "font_names": names,
            "fonts": fonts,
            "effect_names": [],
            "effects": [],
            "property_schemas": {},
            "properties": {},
            "plugin_versions": {},
        },
    }


def _font_root(tmp_path, monkeypatch):
    root = tmp_path / "Fonts"
    root.mkdir(parents=True)
    monkeypatch.setattr(fonts_module, "_system_font_root", lambda: root)
    return root


def test_local_font_path_is_removed_and_hashed_without_mutating_heartbeat(tmp_path, monkeypatch):
    root = _font_root(tmp_path, monkeypatch)
    path = root / "local.ttf"
    payload = _sfnt(postscript="LocalPS", family="Local Family", style="Regular")
    path.write_bytes(payload)
    heartbeat = _heartbeat(
        [
            {
                "match_name": "LocalPS",
                "family": "Local Family",
                "style": "Regular",
                "version": None,
                "local_path": str(path),
            }
        ],
        ["LocalPS"],
    )

    enriched = enrich_heartbeat_fonts(heartbeat)

    font = enriched["capabilities"]["fonts"][0]
    assert "local_path" not in font
    assert font["sha256"] == hashlib.sha256(payload).hexdigest()
    assert font["version_or_hash"] == font["sha256"]
    assert heartbeat["capabilities"]["fonts"][0]["local_path"] == str(path)




def test_local_path_trust_requires_exact_parsed_postscript_name(tmp_path, monkeypatch):
    root = _font_root(tmp_path, monkeypatch)
    path = root / "safe.ttf"
    path.write_bytes(_sfnt(postscript="SafePS", family="Safe Family", style="Regular"))

    fake_only = enrich_heartbeat_fonts(
        _heartbeat(
            [{"match_name": "FakePS", "local_path": str(path)}],
            ["FakePS"],
        )
    )
    assert fake_only["capabilities"]["font_names"] == []

    independently_present = enrich_heartbeat_fonts(
        _heartbeat(
            [
                {"match_name": "FakePS", "local_path": str(path)},
                {"match_name": "SafePS", "local_path": str(path)},
            ],
            ["FakePS", "SafePS"],
        )
    )
    assert independently_present["capabilities"]["font_names"] == ["SafePS"]


def test_invalid_and_symlink_font_paths_are_removed_without_hash(tmp_path, monkeypatch):
    root = _font_root(tmp_path, monkeypatch)
    valid = root / "valid.ttf"
    valid.write_bytes(_sfnt(postscript="ValidPS", family="Family", style="Regular"))
    link = root / "link.ttf"
    try:
        link.symlink_to(valid)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")
    heartbeat = _heartbeat(
        [
            {"match_name": "Missing", "local_path": str(root / "missing.ttf")},
            {"match_name": "Link", "local_path": str(link)},
            {"match_name": "Outside", "local_path": str(tmp_path / "outside.ttf")},
        ],
        ["Missing", "Link", "Outside"],
    )

    enriched = enrich_heartbeat_fonts(heartbeat)
    assert enriched["capabilities"]["font_names"] == []
    assert enriched["capabilities"]["fonts"] == []

    for font in enriched["capabilities"]["fonts"]:
        assert "local_path" not in font
        assert "sha256" not in font


def test_empty_panel_catalog_discovers_exact_names_hashes_and_deduplicates(tmp_path, monkeypatch):
    root = _font_root(tmp_path, monkeypatch)
    zulu = root / "zulu.otf"
    alpha = root / "alpha.ttf"
    zulu_payload = _sfnt(postscript="ZuluPS", family="Zulu Family", style="Bold", version="Version 2.0")
    alpha_payload = _sfnt(postscript="AlphaPS", family="Alpha Family", style="Italic")
    zulu.write_bytes(zulu_payload)
    alpha.write_bytes(alpha_payload)
    heartbeat = _heartbeat([], [])

    enriched = enrich_heartbeat_fonts(heartbeat, font_files=[zulu, alpha, zulu])

    catalog = enriched["capabilities"]
    assert catalog["font_names"] == ["AlphaPS", "ZuluPS"]
    assert [font["match_name"] for font in catalog["fonts"]] == ["AlphaPS", "ZuluPS"]
    assert catalog["fonts"][0]["family"] == "Alpha Family"
    assert catalog["fonts"][0]["style"] == "Italic"
    assert catalog["fonts"][1]["version"] == "Version 2.0"
    assert catalog["fonts"][1]["version_or_hash"] == "Version 2.0"
    assert catalog["fonts"][1]["sha256"] == hashlib.sha256(zulu_payload).hexdigest()
    assert all("local_path" not in font for font in catalog["fonts"])


def test_malformed_discovered_font_is_ignored(tmp_path, monkeypatch):
    root = _font_root(tmp_path, monkeypatch)
    malformed = root / "malformed.ttf"
    malformed.write_bytes(b"not-an-sfnt")

    enriched = enrich_heartbeat_fonts(_heartbeat([], []), font_files=[malformed])

    assert enriched["capabilities"]["font_names"] == []
    assert enriched["capabilities"]["fonts"] == []


def test_adversarial_sfnt_headers_are_ignored_with_bounded_work(tmp_path, monkeypatch):
    root = _font_root(tmp_path, monkeypatch)
    too_many_tables = root / "many-tables.ttf"
    too_many_tables.write_bytes(struct.pack(">IHHHH", 0x00010000, 257, 0, 0, 0))
    too_many_faces = root / "many-faces.ttc"
    too_many_faces.write_bytes(b"ttcf" + struct.pack(">II", 0x00010000, 65) + b"\0" * 260)

    enriched = enrich_heartbeat_fonts(_heartbeat([], []), font_files=[too_many_tables, too_many_faces])

    assert enriched["capabilities"]["font_names"] == []
    assert enriched["capabilities"]["fonts"] == []


def test_pathless_gdi_catalog_merges_hashes_and_deduplicates(tmp_path, monkeypatch):
    root = _font_root(tmp_path, monkeypatch)
    zulu_path = root / "zulu.ttf"
    zulu_payload = _sfnt(postscript="ZuluPS", family="Zulu Family", style="Regular", version="Version 3.0")
    zulu_path.write_bytes(zulu_payload)

    class FakeGDI:
        def __init__(self):
            self.faces = [
                SimpleNamespace(lfFaceName="zulu", lfWeight=400, lfItalic=0, lfCharSet=1),
                SimpleNamespace(lfFaceName="duplicate", lfWeight=400, lfItalic=0, lfCharSet=1),
                SimpleNamespace(lfFaceName="alpha", lfWeight=700, lfItalic=0, lfCharSet=1),
            ]
            self.tables = {
                "zulu": zulu_payload[28:],
                "duplicate": _sfnt(postscript="ZuluPS", family="Other Family", style="Bold")[28:],
                "alpha": _sfnt(postscript="AlphaPS", family="Alpha Family", style="Italic")[28:],
            }
            self.deleted_fonts = 0
            self.deleted_dcs = 0
            self.current = None

        def create_dc(self):
            return "dc"

        def delete_dc(self, dc):
            self.deleted_dcs += 1

        def enumerate(self, dc, callback):
            for face in self.faces:
                if callback(face) == 0:
                    break

        def create_font(self, logfont):
            self.current = logfont
            return logfont

        def select_font(self, dc, font):
            return "old-font"

        def get_name_table(self, dc):
            return self.tables[self.current.lfFaceName]

        def delete_object(self, font):
            self.deleted_fonts += 1

    fake = FakeGDI()
    monkeypatch.setattr(fonts_module, "_load_gdi_api", lambda: fake)
    monkeypatch.setattr(fonts_module, "_registry_font_files", lambda root: [zulu_path])

    enriched = enrich_heartbeat_fonts(_heartbeat([], []))

    catalog = enriched["capabilities"]
    assert catalog["font_names"] == ["AlphaPS", "ZuluPS"]
    assert catalog["fonts"][1]["sha256"] == hashlib.sha256(zulu_payload).hexdigest()
    assert "sha256" not in catalog["fonts"][0]
    assert catalog["fonts"][0]["version_or_hash"] == hashlib.sha256(
        _sfnt(postscript="AlphaPS", family="Alpha Family", style="Italic")[28:]
    ).hexdigest()
    assert fake.deleted_fonts == 3
    assert fake.deleted_dcs == 1




def test_nonempty_panel_catalog_merges_discovered_identity(tmp_path, monkeypatch):
    root = _font_root(tmp_path, monkeypatch)
    safe_path = root / "safe.ttf"
    safe_payload = _sfnt(postscript="SafePS", family="Safe Family", style="Regular")
    safe_path.write_bytes(safe_payload)

    class FakeGDI:
        face = SimpleNamespace(lfFaceName="panel", lfWeight=400, lfItalic=0, lfCharSet=1)

        def create_dc(self):
            return "dc"

        def delete_dc(self, dc):
            return None

        def enumerate(self, dc, callback):
            callback(self.face)

        def create_font(self, logfont):
            return logfont

        def select_font(self, dc, font):
            return "old-font"

        def get_name_table(self, dc):
            return _sfnt(postscript="PanelPS", family="Panel Family", style="Bold")[28:]

        def delete_object(self, font):
            return None

    monkeypatch.setattr(fonts_module, "_load_gdi_api", lambda: FakeGDI())
    monkeypatch.setattr(fonts_module, "_registry_font_files", lambda root: [safe_path])
    heartbeat = _heartbeat(
        [
            {"match_name": "PanelPS", "family": "Panel Family", "style": "Regular", "version": None},
            {
                "match_name": "SafePS",
                "family": "Safe Family",
                "style": "Regular",
                "version": None,
                "local_path": str(safe_path),
            },
            {"match_name": "UnboundPS", "family": "Unknown", "style": "Regular", "version": "Panel supplied 1.0"},
        ],
        ["PanelPS", "SafePS", "UnboundPS"],
    )

    enriched = enrich_heartbeat_fonts(heartbeat)

    catalog = enriched["capabilities"]
    assert catalog["font_names"] == ["PanelPS", "SafePS"]
    panel_font, safe_font = catalog["fonts"]
    assert panel_font["version_or_hash"] == hashlib.sha256(
        _sfnt(postscript="PanelPS", family="Panel Family", style="Bold")[28:]
    ).hexdigest()
    assert "sha256" not in panel_font
    assert safe_font["sha256"] == hashlib.sha256(safe_payload).hexdigest()
    assert safe_font["version_or_hash"] == safe_font["sha256"]
    assert len(catalog["fonts"]) == len(set(catalog["font_names"]))

@pytest.mark.skipif(os.name != "nt", reason="Windows GDI required")
def test_windows_native_font_smoke():
    root = fonts_module._system_font_root()
    assert root is not None
    paths = fonts_module._registry_font_files(root)
    if paths:
        catalog = fonts_module._discover_fonts(paths, root)
        if not catalog:
            pytest.skip("system registry has no readable SFNT font")
        assert catalog[0]["match_name"]
        assert catalog[0]["sha256"]
    gdi_catalog = fonts_module._discover_gdi_fonts()
    assert gdi_catalog
    assert gdi_catalog[0]["match_name"]


def test_capability_model_rejects_blank_font_identity():
    heartbeat = _heartbeat(
        [{"match_name": "BlankPS", "version_or_hash": "   "}],
        ["BlankPS"],
    )

    with pytest.raises(ValueError, match="blank"):
        AECapabilities.from_heartbeat(heartbeat)
