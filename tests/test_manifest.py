"""Manifest and package-shape tests. No Hermes needed."""
from pathlib import Path

import yaml

import zalo

MANIFEST = Path(__file__).resolve().parents[1] / "zalo" / "plugin.yaml"
OPTIONAL_ENV = {
    "ZALO_MODE", "ZALO_POLL_TIMEOUT", "ZALO_WEBHOOK_URL", "ZALO_WEBHOOK_SECRET",
    "ZALO_WEBHOOK_HOST", "ZALO_WEBHOOK_PORT", "ZALO_ALLOWED_USERS", "ZALO_ALLOW_ALL_USERS",
    "ZALO_ALLOWED_GROUPS", "ZALO_HOME_CHANNEL", "ZALO_HOME_CHANNEL_NAME",
}


def _manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text())


def test_manifest_identity():
    data = _manifest()
    assert data["name"] == "zalo"
    assert data["kind"] == "platform"
    assert data["version"] == "0.1.0"
    assert data["label"] == "Zalo Bot"


def test_manifest_requires_only_the_token():
    entries = _manifest()["requires_env"]
    assert [e["name"] for e in entries] == ["ZALO_BOT_TOKEN"]
    assert entries[0]["password"] is True
    assert entries[0]["url"].startswith("https://bot.zapps.me/")


def test_manifest_optional_env_names():
    entries = _manifest()["optional_env"]
    assert {e["name"] for e in entries} == OPTIONAL_ENV
    secret = next(e for e in entries if e["name"] == "ZALO_WEBHOOK_SECRET")
    assert secret["password"] is True


def test_package_exposes_lazy_register():
    assert callable(zalo.register)
