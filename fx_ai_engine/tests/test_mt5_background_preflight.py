from __future__ import annotations

from validation import mt5_background_preflight


def test_probe_reports_candidates_without_assuming_account_or_active_path(tmp_path, monkeypatch):
    home = tmp_path / 'user'
    apps = tmp_path / 'Applications'
    (apps / 'MetaTrader 5.app').mkdir(parents=True)
    terminal = home / 'Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/Program Files/MetaTrader 5'
    (terminal / 'MQL5').mkdir(parents=True)
    (terminal / 'terminal64.exe').touch()
    (terminal / 'metaeditor64.exe').touch()
    monkeypatch.setattr(mt5_background_preflight.importlib.util, 'find_spec', lambda name: None)
    report = mt5_background_preflight.probe(home=home, applications=apps, platform_name='Darwin',
                                             env={'MT5_PASSWORD': 'do-not-expose'})
    assert report['terminal_app_installed'] and report['metaeditor_binary_found']
    assert report['mql5_candidates'] == [str(terminal / 'MQL5')]
    assert not report['active_terminal_data_path_verified']
    assert not report['account_type_verified'] and not report['native_mt5_binding_available']
    assert 'do-not-expose' not in str(report)


def test_probe_never_creates_bridge_or_terminal_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(mt5_background_preflight.importlib.util, 'find_spec', lambda name: None)
    missing = tmp_path / 'missing-bridge'
    report = mt5_background_preflight.probe(home=tmp_path, applications=tmp_path, platform_name='Darwin',
                                             env={'BRIDGE_BASE_PATH': str(missing)})
    assert report['bridge_override_configured'] and not report['bridge_override_exists']
    assert not missing.exists()


def test_probe_finds_wine_users_other_than_host_username(tmp_path, monkeypatch):
    home = tmp_path / 'raynj'
    wine = home / 'Library/Application Support/net.metaquotes.wine.metatrader5/drive_c'
    mql = wine / 'users/user/AppData/Roaming/MetaQuotes/Terminal/INSTANCE/MQL5'
    mql.mkdir(parents=True)
    monkeypatch.setattr(mt5_background_preflight.importlib.util, 'find_spec', lambda name: None)
    report = mt5_background_preflight.probe(home=home, applications=tmp_path, platform_name='Darwin', env={})
    assert str(mql) in report['mql5_candidates']
    assert not report['active_terminal_data_path_verified']
