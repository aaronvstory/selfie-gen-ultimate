from pathlib import Path

import numpy as np
import pytest

import face_crop_service as svc


def test_extract_portrait_crop_raises_on_imwrite_false(tmp_path: Path, monkeypatch):
    input_path = tmp_path / "front.png"
    output_path = tmp_path / "extracted.png"
    input_path.write_bytes(b"x")

    fake_image = np.zeros((120, 120, 3), dtype=np.uint8)
    monkeypatch.setattr(svc.cv2, "imread", lambda _p: fake_image)
    monkeypatch.setattr(svc, "_detect_face_box_opencv", lambda _img: (20, 20, 40, 40))
    monkeypatch.setattr(svc.cv2, "imwrite", lambda _p, _img: False)

    with pytest.raises(RuntimeError, match="Failed to write portrait crop"):
        svc.extract_portrait_crop(str(input_path), str(output_path))
