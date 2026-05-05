from types import SimpleNamespace
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


def _mock_ephemeral_root(monkeypatch) -> DummyRoot:
    root = DummyRoot()
    monkeypatch.setattr(tk_dialogs.tk, "Tk", lambda: root)
    return root


def test_select_directory_parent_path(monkeypatch, tmp_path):
    called = {"kwargs": None}

    def fake_dialog(**kwargs):
        called["kwargs"] = kwargs
        return str(tmp_path / "example")

    parent = object()
    monkeypatch.setattr(tk_dialogs.filedialog, "askdirectory", fake_dialog)
    out = tk_dialogs.select_directory(parent=parent, title="Pick")
    assert out == str(tmp_path / "example")
    assert called["kwargs"]["parent"] is parent


def test_select_open_file_cancel_normalized(monkeypatch):
    _mock_ephemeral_root(monkeypatch)
    monkeypatch.setattr(tk_dialogs.filedialog, "askopenfilename", lambda **_kwargs: "")
    assert tk_dialogs.select_open_file(title="Pick") is None


def test_select_open_files_normalizes_to_list(monkeypatch):
    _mock_ephemeral_root(monkeypatch)
    monkeypatch.setattr(tk_dialogs.filedialog, "askopenfilenames", lambda **_kwargs: ("a.png", "b.png"))
    assert tk_dialogs.select_open_files(title="Pick") == ["a.png", "b.png"]


def test_select_open_files_cancel_normalized(monkeypatch):
    _mock_ephemeral_root(monkeypatch)
    monkeypatch.setattr(tk_dialogs.filedialog, "askopenfilenames", lambda **_kwargs: ())
    assert tk_dialogs.select_open_files(title="Pick") == []


def test_select_save_file_cancel_normalized(monkeypatch):
    _mock_ephemeral_root(monkeypatch)
    monkeypatch.setattr(tk_dialogs.filedialog, "asksaveasfilename", lambda **_kwargs: "")
    assert tk_dialogs.select_save_file(title="Save") is None


def test_ephemeral_root_destroyed_on_success(monkeypatch, tmp_path):
    root = _mock_ephemeral_root(monkeypatch)
    monkeypatch.setattr(tk_dialogs.filedialog, "askdirectory", lambda **_kwargs: str(tmp_path / "folder"))
    assert tk_dialogs.select_directory(title="Pick") == str(tmp_path / "folder")
    assert root.destroyed is True


def test_ephemeral_root_destroyed_on_dialog_exception(monkeypatch):
    root = _mock_ephemeral_root(monkeypatch)

    def _boom(**_kwargs):
        raise RuntimeError("dialog failed")

    monkeypatch.setattr(tk_dialogs.filedialog, "askdirectory", _boom)
    out = tk_dialogs.select_directory(title="Pick")
    assert out is None
    assert root.destroyed is True


def test_prepare_root_for_cli_does_not_apply_focus_hack_on_darwin(monkeypatch):
    root = DummyRoot()
    monkeypatch.setattr(tk_dialogs.sys, "platform", "darwin")

    calls = {"lift": 0, "focus": 0, "attributes": 0}

    def _track_lift():
        calls["lift"] += 1
        return None

    def _track_attributes(*args):
        calls["attributes"] += 1
        return None

    def _track_focus():
        calls["focus"] += 1
        return None

    root.lift = _track_lift
    root.attributes = _track_attributes
    root.focus_force = _track_focus

    tk_dialogs._prepare_root_for_cli(root)

    assert calls["lift"] == 0
    assert calls["focus"] == 0
    assert calls["attributes"] == 0


def test_run_dialog_logs_picker_start_end(monkeypatch, tmp_path):
    root = _mock_ephemeral_root(monkeypatch)
    monkeypatch.setattr(tk_dialogs.filedialog, "askdirectory", lambda **_kwargs: str(tmp_path / "example"))
    captured = []

    monkeypatch.setattr(tk_dialogs.logger, "info", lambda message, *args: captured.append((message, args)))
    out = tk_dialogs.select_directory(title="Pick")

    assert out == str(tmp_path / "example")
    assert root.destroyed is True
    messages = [item[0] for item in captured]
    assert any("picker_start" in m for m in messages)
    assert any("picker_end" in m for m in messages)


def test_run_dialog_logs_picker_error(monkeypatch):
    _mock_ephemeral_root(monkeypatch)

    def _boom(**_kwargs):
        raise RuntimeError("dialog failed")

    monkeypatch.setattr(tk_dialogs.filedialog, "askdirectory", _boom)
    captured = []
    monkeypatch.setattr(tk_dialogs.logger, "error", lambda message, *args: captured.append((message, args)))

    out = tk_dialogs.select_directory(title="Pick")
    assert out is None
    assert any("picker_error" in m for m, _ in captured)


def test_select_directory_cli_safe_uses_osascript_on_darwin(monkeypatch):
    monkeypatch.setattr(tk_dialogs.sys, "platform", "darwin")
    monkeypatch.setattr(
        tk_dialogs.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="/tmp/folder\n", stderr=""),
    )
    out = tk_dialogs.select_directory_cli_safe(title="Pick Folder")
    assert out == "/tmp/folder"


def test_select_directory_cli_safe_cancel_on_darwin(monkeypatch):
    monkeypatch.setattr(tk_dialogs.sys, "platform", "darwin")
    monkeypatch.setattr(
        tk_dialogs.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="User canceled."),
    )
    out = tk_dialogs.select_directory_cli_safe(title="Pick Folder")
    assert out is None


def test_select_directory_cli_safe_non_darwin_delegates(monkeypatch, tmp_path):
    monkeypatch.setattr(tk_dialogs.sys, "platform", "linux")
    monkeypatch.setattr(
        tk_dialogs,
        "select_directory",
        lambda **kwargs: str(tmp_path / "picked"),
    )
    out = tk_dialogs.select_directory_cli_safe(title="Pick Folder")
    assert out == str(tmp_path / "picked")
