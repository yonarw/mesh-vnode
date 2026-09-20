"""VNODE_CONFIG_FILE: the YAML/JSON settings source.

The Home Assistant add-on has no translation layer - the Supervisor writes
/data/options.json and the image points VNODE_CONFIG_FILE at it - so the
precedence and the missing-file behaviour are load-bearing, not cosmetic.
"""

from __future__ import annotations

import json

import pytest

from mesh_vnode.config import Settings, config_file


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    # chdir away from the repo so a developer's own .env cannot colour a run.
    monkeypatch.chdir(tmp_path)
    for name in list(__import__("os").environ):
        if name.startswith("VNODE_"):
            monkeypatch.delenv(name, raising=False)


def write_json(tmp_path, **values):
    path = tmp_path / "options.json"
    path.write_text(json.dumps(values))
    return path


def test_json_file_is_read(monkeypatch, tmp_path):
    path = write_json(tmp_path, upstream_host="192.0.2.10", replay_mode="none", retention_days=7)
    monkeypatch.setenv("VNODE_CONFIG_FILE", str(path))

    s = Settings()

    assert s.upstream_host == "192.0.2.10"
    assert s.replay_mode == "none"
    assert s.retention_days == 7


def test_yaml_file_is_read(monkeypatch, tmp_path):
    path = tmp_path / "vnode.yaml"
    path.write_text("upstream_host: 192.0.2.20\nreplay_limit: 42\nlog_level: DEBUG\n")
    monkeypatch.setenv("VNODE_CONFIG_FILE", str(path))

    s = Settings()

    assert s.upstream_host == "192.0.2.20"
    assert s.replay_limit == 42
    assert s.log_level == "DEBUG"


def test_extension_decides_the_parser(monkeypatch, tmp_path):
    """Supervisor's file is .json; a hand-written one is usually .yaml. Anything
    else falls back to JSON rather than guessing."""
    path = tmp_path / "options"
    path.write_text(json.dumps({"upstream_port": 4405}))
    monkeypatch.setenv("VNODE_CONFIG_FILE", str(path))

    assert Settings().upstream_port == 4405


def test_environment_beats_the_file(monkeypatch, tmp_path):
    """One image serves both deployments: a VNODE_* baked into it or set in a
    compose file still wins over whatever the file says."""
    path = write_json(tmp_path, upstream_host="192.0.2.10", replay_limit=42)
    monkeypatch.setenv("VNODE_CONFIG_FILE", str(path))
    monkeypatch.setenv("VNODE_UPSTREAM_HOST", "192.0.2.99")

    s = Settings()

    assert s.upstream_host == "192.0.2.99"
    # Only the overridden key; the rest of the file still applies.
    assert s.replay_limit == 42


def test_missing_file_is_not_an_error(monkeypatch, tmp_path):
    """The add-on image sets the path unconditionally and docker-compose has no
    options.json, so absence has to be silent - and leave the defaults alone."""
    monkeypatch.setenv("VNODE_CONFIG_FILE", str(tmp_path / "nope.json"))

    assert Settings().upstream_host == "meshtastic.local"
    assert config_file() is None


def test_unknown_keys_are_ignored(monkeypatch, tmp_path):
    """options.json carries whatever the add-on schema declares; a key we do not
    have must not stop the service from starting."""
    path = write_json(tmp_path, upstream_host="192.0.2.10", something_we_dropped=True)

    monkeypatch.setenv("VNODE_CONFIG_FILE", str(path))

    assert Settings().upstream_host == "192.0.2.10"


def test_config_file_reports_what_was_used(monkeypatch, tmp_path):
    path = write_json(tmp_path, upstream_host="192.0.2.10")
    monkeypatch.setenv("VNODE_CONFIG_FILE", str(path))
    assert config_file() == path

    monkeypatch.delenv("VNODE_CONFIG_FILE")
    assert config_file() is None
