import types

import tk_dialogs


class DummyRoot:
    def __init__(self):
        self.destroyed = False

    def withdraw(self):
        return None

    def update_idletasks(self):
        return None

    def lift(self):
        return None

    def attributes(self, *_args, **_kwargs):
        return None

    def focus_force(self):
        return None

    def after_idle(self, callback):
        callback()

    def destroy(self):
        self.destroyed = True


def test_select_directory_parent_path(monkeypatch):
    called = {"kwargs": None}

    def fake_dialog(**kwargs):
        called["kwargs"] = kwargs
        return "/tmp/example"

    parent = object()
    monkeypatch.setattr(tk_dialogs.filedialog, "askdirectory", fake_dialog)
    out = tk_dialogs.select_directory(parent=parent, title="Pick")
    assert out == "/tmp/example"
    assert called["kwargs"]["parent"] is parent


def test_select_open_file_cancel_normalized(monkeypatch):
    monkeypatch.setattr(tk_dialogs.filedialog, "askopenfilename", lambda **_kwargs: "")
    assert tk_dialogs.select_open_file(title="Pick") is None


def test_select_open_files_normalizes_to_list(monkeypatch):
    monkeypatch.setattr(tk_dialogs.filedialog, "askopenfilenames", lambda **_kwargs: ("a.png", "b.png"))
    assert tk_dialogs.select_open_files(title="Pick") == ["a.png", "b.png"]


def test_select_save_file_cancel_normalized(monkeypatch):
    monkeypatch.setattr(tk_dialogs.filedialog, "asksaveasfilename", lambda **_kwargs: None)
    assert tk_dialogs.select_save_file(title="Save") is None


def test_ephemeral_root_destroyed_on_success(monkeypatch):
    root = DummyRoot()
    monkeypatch.setattr(tk_dialogs.tk, "Tk", lambda: root)
    monkeypatch.setattr(tk_dialogs.filedialog, "askdirectory", lambda **_kwargs: "/tmp/folder")
    assert tk_dialogs.select_directory(title="Pick") == "/tmp/folder"
    assert root.destroyed is True


def test_ephemeral_root_destroyed_on_dialog_exception(monkeypatch):
    root = DummyRoot()
    monkeypatch.setattr(tk_dialogs.tk, "Tk", lambda: root)

    def _boom(**_kwargs):
        raise RuntimeError("dialog failed")

    monkeypatch.setattr(tk_dialogs.filedialog, "askdirectory", _boom)
    out = tk_dialogs.select_directory(title="Pick")
    assert out is None
    assert root.destroyed is True
