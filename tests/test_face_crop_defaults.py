from pathlib import Path


def test_face_crop_default_expands_generative_expand():
    text = Path("kling_gui/tabs/face_crop_tab.py").read_text(encoding="utf-8")
    assert 'self._expanded_sections = ["expand"]' in text
