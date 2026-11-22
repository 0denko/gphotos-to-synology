"""Comprehensive unit tests for Settings and ConfigManager (M1)."""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from gphotos_synology.config import (
    DEFAULT_SYNOLOGY_URL,
    MASK_SENTINEL,
    MUTABLE_UI_FIELDS,
    SECRET_FIELDS,
    ConfigManager,
    Settings,
    validate_synology_url,
)


def _make_base_settings(tmp_path: Path, **overrides) -> Settings:
    defaults = {
        "data_dir": tmp_path / "data",
        "work_dir": tmp_path / "work",
        "inbox_dir": tmp_path / "inbox",
        "photos_dir": tmp_path / "photos",
        "homes_dir": tmp_path / "homes",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def test_secure_defaults_g5_and_c1(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "APP_HOST",
        "SYNOLOGY_URL",
        "SYNOLOGY_VERIFY_SSL",
        "SYNOLOGY_CA_CERT",
        "QUARANTINE_DIR",
        "QUARANTINE_DIR_OVERRIDE",
        "GPHOTOS_PROXY_SECRET",
        "PROXY_SECRET",
        "PROXY_SECRET_FILE",
        "GPHOTOS_PROXY_SECRET_FILE",
        "API_TOKEN",
        "APP_API_TOKEN",
        "DSM_ADMIN_USERS",
        "TRUST_PROXY_HEADERS",
        "ALLOW_INSECURE_NO_AUTH",
    ):
        monkeypatch.delenv(var, raising=False)

    s = Settings(_env_file=None)
    assert s.app_host == "127.0.0.1"
    assert s.synology_url == DEFAULT_SYNOLOGY_URL == "https://127.0.0.1:5001"
    assert s.synology_verify_ssl is True
    assert s.synology_ca_cert == ""
    assert s.perceptual_max_distance == 10
    assert s.quarantine_dir_override is None
    assert s.quarantine_dir == s.work_dir / "replaced_duplicates"
    assert s.proxy_secret == ""
    assert s.proxy_secret_file is None
    assert s.api_token == ""
    assert s.dsm_admin_users == ""
    assert s.trust_proxy_headers is False
    assert s.allow_insecure_no_auth is False


def test_env_aliases_blank_paths_roundtrip_and_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    custom_q = tmp_path / "external_quarantine"
    secret_file = tmp_path / "proxy.secret"
    secret_file.write_text("  file-secret-value \n", encoding="utf-8")

    monkeypatch.setenv("QUARANTINE_DIR", str(custom_q))
    monkeypatch.setenv("GPHOTOS_PROXY_SECRET", "  env-secret-value  ")
    monkeypatch.setenv("PROXY_SECRET_FILE", str(secret_file))
    monkeypatch.setenv("API_TOKEN", "tok-123")
    monkeypatch.setenv("DSM_ADMIN_USERS", " Admin , Alice, ,BOB ")

    s = Settings(_env_file=None, work_dir=tmp_path / "work")
    assert s.quarantine_dir_override == custom_q
    assert s.quarantine_dir == custom_q
    assert s.proxy_secret == "  env-secret-value  "
    assert s.proxy_secret_file == secret_file
    assert s.api_token == "tok-123"
    assert s.resolve_proxy_secret() == "env-secret-value"
    assert s.dsm_admin_user_set() == {"admin", "alice", "bob"}

    # Direct python kwargs + model_validate(model_dump()) roundtrip must not re-read env
    explicit_none = Settings(
        _env_file=None,
        work_dir=tmp_path / "work",
        quarantine_dir_override=None,
        proxy_secret="",
        proxy_secret_file=None,
        api_token="",
    )
    roundtripped = Settings.model_validate(explicit_none.model_dump())
    assert roundtripped.quarantine_dir_override is None
    assert roundtripped.quarantine_dir == tmp_path / "work" / "replaced_duplicates"
    assert roundtripped.proxy_secret == ""
    assert roundtripped.proxy_secret_file is None
    assert roundtripped.api_token == ""

    # Blank strings in env coerce to None (not Path("."))
    monkeypatch.setenv("QUARANTINE_DIR", "   ")
    monkeypatch.setenv("GPHOTOS_PROXY_SECRET", "")
    monkeypatch.setenv("PROXY_SECRET_FILE", "")
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("APP_API_TOKEN", "fallback-tok")
    s_blank = Settings(_env_file=None, work_dir=tmp_path / "work")
    assert s_blank.quarantine_dir_override is None
    assert s_blank.quarantine_dir == tmp_path / "work" / "replaced_duplicates"
    assert s_blank.proxy_secret_file is None
    assert s_blank.api_token == "fallback-tok"

    # Secondary aliases: PROXY_SECRET and GPHOTOS_PROXY_SECRET_FILE
    monkeypatch.delenv("GPHOTOS_PROXY_SECRET", raising=False)
    monkeypatch.delenv("PROXY_SECRET_FILE", raising=False)
    monkeypatch.setenv("GPHOTOS_PROXY_SECRET_FILE", str(secret_file))
    s_file = Settings(_env_file=None)
    assert s_file.proxy_secret_file == secret_file
    assert s_file.resolve_proxy_secret() == "file-secret-value"

    monkeypatch.setenv("PROXY_SECRET", "direct-alias-secret")
    s_direct = Settings(_env_file=None)
    assert s_direct.resolve_proxy_secret() == "direct-alias-secret"

    # Missing proxy_secret_file fails closed with WARNING
    missing_file = tmp_path / "does_not_exist.secret"
    s_missing = Settings(_env_file=None, proxy_secret="", proxy_secret_file=missing_file)
    with caplog.at_level(logging.WARNING):
        assert s_missing.resolve_proxy_secret() == ""
    assert any("proxy_secret_file" in rec.message for rec in caplog.records)


def test_mutable_allowlist_and_secret_masking(tmp_path: Path) -> None:
    forbidden_from_ui = {
        "proxy_secret",
        "proxy_secret_file",
        "api_token",
        "dsm_admin_users",
        "trust_proxy_headers",
        "allow_insecure_no_auth",
        "synology_ca_cert",
        "quarantine_dir_override",
        "photos_dir",
        "data_dir",
        "work_dir",
        "inbox_dir",
        "homes_dir",
        "app_host",
        "app_port",
        "exiftool_path",
        "github_repo",
        "watchtower_url",
        "watchtower_token",
    }
    assert MUTABLE_UI_FIELDS.isdisjoint(forbidden_from_ui)
    assert {"proxy_secret", "api_token", "synology_password", "google_client_secret"}.issubset(
        SECRET_FIELDS
    )

    base = _make_base_settings(
        tmp_path,
        proxy_secret="my-proxy-secret",
        api_token="my-api-token",
        watchtower_token="my-wt-token",
        google_client_secret="my-g-secret",
        synology_password="my-dsm-password",
        smtp_password="my-smtp-password",
    )
    cfg = ConfigManager(base)

    # Attempting to overwrite non-mutable fields via update() must be ignored
    cfg.update(
        {
            "proxy_secret": "hacked",
            "api_token": "hacked",
            "allow_insecure_no_auth": True,
            "trust_proxy_headers": True,
            "dsm_admin_users": "evil",
            "photos_dir": "/tmp/evil",
            "watchtower_url": "http://evil.invalid",
            "poll_interval_minutes": 45,
        }
    )
    assert cfg.settings.proxy_secret == "my-proxy-secret"
    assert cfg.settings.api_token == "my-api-token"
    assert cfg.settings.allow_insecure_no_auth is False
    assert cfg.settings.trust_proxy_headers is False
    assert cfg.settings.dsm_admin_users == ""
    assert cfg.settings.photos_dir == tmp_path / "photos"
    assert cfg.settings.poll_interval_minutes == 45

    # Even if a non-mutable key was tampered on disk, update() filters disk overrides
    raw_disk = json.loads(base.runtime_config_path.read_text(encoding="utf-8"))
    raw_disk["allow_insecure_no_auth"] = True
    base.runtime_config_path.write_text(json.dumps(raw_disk), encoding="utf-8")
    cfg.update({"poll_interval_minutes": 30})
    persisted = json.loads(base.runtime_config_path.read_text(encoding="utf-8"))
    assert "allow_insecure_no_auth" not in persisted
    assert cfg.settings.allow_insecure_no_auth is False

    public = cfg.to_public_dict()
    for field_name in SECRET_FIELDS:
        assert public[field_name] == MASK_SENTINEL


@pytest.mark.parametrize(
    "bad_url",
    [
        "",
        None,
        123,
        "ftp://nas.local:5000",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https://",
        "https:///path",
        "http://:5000",
        "https://nas.local:99999",
        "https://nas.local:notaport",
        "https://nas.local:5001 extra",
        " https://nas.local:5001",
        "https://nas.local:5001\r\nX-Injected: 1",
        "https://nas.local:5001\x00",
    ],
)
def test_synology_url_validation_rejects_malformed_values(
    tmp_path: Path, bad_url: object
) -> None:
    with pytest.raises(ValueError):
        validate_synology_url(bad_url)

    base = _make_base_settings(tmp_path, synology_password="keep-me")
    cfg = ConfigManager(base)
    cfg.last_update_warnings = ["sentinel-warning"]

    with pytest.raises(ValueError):
        cfg.update({"synology_url": bad_url, "poll_interval_minutes": 12})

    # Memory, disk, and prior warnings must remain untouched
    assert cfg.settings.synology_url == DEFAULT_SYNOLOGY_URL
    assert cfg.settings.synology_password == "keep-me"
    assert cfg.settings.poll_interval_minutes == 60
    assert cfg.last_update_warnings == ["sentinel-warning"]
    assert not base.runtime_config_path.exists()


def test_settings_allows_empty_synology_url_but_rejects_invalid_non_empty(tmp_path: Path) -> None:
    empty = _make_base_settings(tmp_path, synology_url="")
    assert empty.synology_url == ""

    with pytest.raises(ValidationError):
        _make_base_settings(tmp_path, synology_url="ftp://nas.local:5000")


def test_g4_synology_url_change_clears_password_including_env_var(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Case 1: Password originated from base_settings / env var
    base = _make_base_settings(
        tmp_path,
        synology_url="https://nas1.local:5001",
        synology_password="env-dsm-password",
    )
    cfg = ConfigManager(base)

    # Unchanged URL (trailing slash / case diff) preserves password and produces no warning
    cfg.update({"synology_url": "HTTPS://NAS1.LOCAL:5001/", "synology_password": MASK_SENTINEL})
    assert cfg.settings.synology_password == "env-dsm-password"
    assert cfg.last_update_warnings == []

    # Changing URL with omitted password clears env-var password and persists ""
    with caplog.at_level(logging.WARNING):
        cfg.update({"synology_url": "https://attacker.example:5001"})
    assert cfg.settings.synology_url == "https://attacker.example:5001"
    assert cfg.settings.synology_password == ""
    assert len(cfg.last_update_warnings) == 1
    assert any("Cleared stored synology_password" in rec.message for rec in caplog.records)

    # Reloading ConfigManager with same env base_settings must keep password cleared!
    reloaded = ConfigManager(base)
    assert reloaded.settings.synology_url == "https://attacker.example:5001"
    assert reloaded.settings.synology_password == ""

    # Case 2: Changing URL when NO prior password was set produces no spurious warning
    reloaded.update({"synology_url": "https://nas2.local:5001"})
    assert reloaded.settings.synology_password == ""
    assert reloaded.last_update_warnings == []

    # Case 3: Set runtime password, then change URL with masked sentinel "********" -> cleared
    reloaded.update({"synology_password": "runtime-password"})
    assert reloaded.settings.synology_password == "runtime-password"
    reloaded.update(
        {
            "synology_url": "https://nas3.local:5001",
            "synology_password": MASK_SENTINEL,
        }
    )
    assert reloaded.settings.synology_password == ""
    assert len(reloaded.last_update_warnings) == 1

    # Case 4: Set password, then change URL WITH fresh plaintext password -> kept & warnings reset
    reloaded.update({"synology_password": "old-pw"})
    reloaded.update(
        {
            "synology_url": "https://nas4.local:5001",
            "synology_password": "fresh-pw",
        }
    )
    assert reloaded.settings.synology_url == "https://nas4.local:5001"
    assert reloaded.settings.synology_password == "fresh-pw"
    assert reloaded.last_update_warnings == []


def test_lazy_oauth_and_url_inheritance_and_password_pinning(tmp_path: Path) -> None:
    default_cfg = ConfigManager(_make_base_settings(tmp_path / "default"))
    # Pass fallback_settings_provider positionally to verify positional + keyword compatibility
    child_cfg = ConfigManager(
        _make_base_settings(tmp_path / "maria"),
        lambda: default_cfg.settings,
    )

    # Child created BEFORE default sets creds sees them immediately once default updates
    assert child_cfg.settings.google_client_id == ""
    default_cfg.update(
        {
            "google_client_id": "client-v1",
            "google_client_secret": "secret-v1",
            "synology_url": "https://nas-v1.local:5001",
        }
    )
    assert child_cfg.settings.google_client_id == "client-v1"
    assert child_cfg.settings.google_client_secret == "secret-v1"
    assert child_cfg.settings.synology_url == "https://nas-v1.local:5001"

    pub = child_cfg.to_public_dict()
    assert pub["google_client_id"] == "client-v1"
    assert pub["google_client_secret"] == MASK_SENTINEL
    assert pub["synology_url"] == "https://nas-v1.local:5001"

    # Full-form UI save on child sending back inherited values must NOT freeze inheritance
    child_cfg.update(
        {
            "poll_interval_minutes": 25,
            "google_client_id": "client-v1",
            "google_client_secret": MASK_SENTINEL,
            "synology_url": "https://nas-v1.local:5001",
        }
    )
    assert child_cfg.settings.poll_interval_minutes == 25

    # Rotating default credentials immediately propagates to child
    default_cfg.update(
        {
            "google_client_id": "client-v2",
            "google_client_secret": "secret-v2",
            "synology_url": "https://nas-v2.local:5001",
        }
    )
    assert child_cfg.settings.google_client_id == "client-v2"
    assert child_cfg.settings.google_client_secret == "secret-v2"
    assert child_cfg.settings.synology_url == "https://nas-v2.local:5001"

    # Child saving a non-empty synology_password pins effective synology_url locally
    child_cfg.update({"synology_password": "maria-dsm-pw"})
    assert child_cfg.settings.synology_url == "https://nas-v2.local:5001"
    assert child_cfg.settings.synology_password == "maria-dsm-pw"

    # Subsequent default synology_url change must NOT redirect maria's stored password
    default_cfg.update({"synology_url": "https://nas-v3.local:5001"})
    assert child_cfg.settings.synology_url == "https://nas-v2.local:5001"
    assert child_cfg.settings.synology_password == "maria-dsm-pw"

    # Explicit distinct google_client_id overrides default
    child_cfg.update({"google_client_id": "maria-own-client"})
    assert child_cfg.settings.google_client_id == "maria-own-client"
    default_cfg.update({"google_client_id": "client-v3"})
    assert child_cfg.settings.google_client_id == "maria-own-client"


@pytest.mark.parametrize(
    "payload",
    [
        b'{"poll_interval_minutes": ',  # truncated JSON
        b"\xff\xfe\x00\x80invalid-utf8",  # invalid UTF-8 bytes
        b"[1, 2, 3]",  # non-dict JSON
        b'{"poll_interval_minutes": -5}',  # schema-invalid value
    ],
)
def test_corrupt_runtime_settings_backed_up_and_fallback_to_base(
    tmp_path: Path,
    payload: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    base = _make_base_settings(tmp_path, poll_interval_minutes=75)
    base.ensure_directories()
    base.runtime_config_path.write_bytes(payload)

    with caplog.at_level(logging.ERROR):
        cfg = ConfigManager(base)

    assert cfg.settings.poll_interval_minutes == 75
    backups = sorted(base.data_dir.glob("runtime_settings.json.corrupt.*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == payload
    assert not base.runtime_config_path.exists()
    assert any("Corrupt runtime settings" in rec.message for rec in caplog.records)


def test_atomic_write_permissions_and_chmod_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    base = _make_base_settings(tmp_path)
    cfg = ConfigManager(base)
    cfg.update({"poll_interval_minutes": 42})

    assert base.runtime_config_path.exists()
    mode = stat.S_IMODE(os.stat(base.runtime_config_path).st_mode)
    assert mode == 0o600
    assert list(base.data_dir.glob("*.tmp")) == []

    # Monkeypatch os.chmod to raise PermissionError: write still succeeds and logs WARNING
    def _fail_chmod(_path: object, _mode: int) -> None:
        raise PermissionError("chmod blocked by filesystem")

    monkeypatch.setattr(os, "chmod", _fail_chmod)
    with caplog.at_level(logging.WARNING):
        cfg.update({"poll_interval_minutes": 55})

    assert cfg.settings.poll_interval_minutes == 55
    assert json.loads(base.runtime_config_path.read_text(encoding="utf-8"))[
        "poll_interval_minutes"
    ] == 55
    assert any("0600" in rec.message for rec in caplog.records)
