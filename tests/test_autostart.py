import plistlib

from utils import autostart


def test_mac_autostart_install_and_remove(tmp_path, monkeypatch):
    plist_path = tmp_path / "com.formatmaster.app.plist"
    calls = []

    monkeypatch.setattr(autostart.sys, "platform", "darwin")
    monkeypatch.setattr(autostart, "_mac_plist_path", lambda: str(plist_path))
    monkeypatch.setattr(
        autostart, "_launchctl", lambda args: calls.append(args) or True)
    monkeypatch.setattr(autostart.sys, "frozen", False, raising=False)

    assert autostart.set_mac_autostart(True) is True
    assert autostart.mac_autostart_enabled() is True
    with plist_path.open("rb") as stream:
        payload = plistlib.load(stream)
    assert payload["Label"] == "com.formatmaster.app"
    assert payload["RunAtLoad"] is True
    assert payload["LimitLoadToSessionType"] == "Aqua"
    assert payload["ProgramArguments"][-1].endswith("main_qt.py")
    assert any(args[0] == "bootstrap" for args in calls)

    assert autostart.set_mac_autostart(False) is True
    assert autostart.mac_autostart_enabled() is False
    assert any(args[0] == "bootout" for args in calls)


def test_autostart_is_disabled_outside_macos(monkeypatch):
    monkeypatch.setattr(autostart.sys, "platform", "linux")

    assert autostart.mac_autostart_enabled() is False
    assert autostart.set_mac_autostart(True) is False
