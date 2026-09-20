"""Runtime configuration. Everything is settable through VNODE_* environment
variables so the service can be deployed with docker/systemd without a config file.

A config file is optional: point VNODE_CONFIG_FILE at a YAML or JSON file whose
top-level keys are the field names below, unprefixed. YAML suits a hand-written
file; JSON is what the Home Assistant Supervisor writes to /data/options.json,
so the add-on needs no translation layer of its own.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic_settings import (
    BaseSettings,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

ReplayMode = Literal["cursor", "none"]
ClientKeyMode = Literal["ip", "ip_port", "shared"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VNODE_", env_file=".env", extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Add the VNODE_CONFIG_FILE source, below the environment.

        Below on purpose: a VNODE_* variable baked into an image or set in a
        compose file still wins over the file, which is what makes one image
        serve both deployments. A path that is not there is simply not a source
        - the add-on image points this at /data/options.json unconditionally,
        and outside Home Assistant that file does not exist. `config_file()`
        reports what was actually picked up, and the CLI logs it at startup.
        """
        sources = [init_settings, env_settings, dotenv_settings]
        path = config_file()
        if path is not None:
            sources.append(_file_source(settings_cls, path))
        sources.append(file_secret_settings)
        return tuple(sources)

    # --- upstream: the real node ---
    upstream_host: str = "meshtastic.local"
    upstream_port: int = 4403

    # --- downstream: the phone app ---
    listen_host: str = "0.0.0.0"
    listen_port: int = 4404

    # --- web ui ---
    web_host: str = "0.0.0.0"
    web_port: int = 8080

    db_path: Path = Path("data/vnode.sqlite3")

    # --- replay behaviour ---
    # cursor : per-client cursor, only what that client has not seen
    # none   : no replay at all; the virtual node is then a plain multiplexer
    replay_mode: ReplayMode = "cursor"
    replay_limit: int = 200
    replay_max_age_days: int = 30
    # Milliseconds between replayed frames; some clients choke on a burst.
    replay_pace_ms: int = 20
    # Do not replay a client's own sent messages back to it.
    replay_skip_own: bool = True

    # How a reconnecting client is recognised again.
    client_key_mode: ClientKeyMode = "ip"

    # --- forwarding / safety ---
    allow_admin: bool = False
    # Admin *read* requests (get_config, get_owner, ...) pass even with admin
    # blocked: the app asks for them right after connecting, and the answers
    # are what the handshake already gave it. Writes stay blocked.
    allow_admin_reads: bool = True

    # Store messages the phone sends, so the web UI and other clients see them.
    store_outgoing: bool = True

    retention_days: int = 30
    client_idle_timeout_s: int = 900

    # --- logging (see logging_setup.py) ---
    log_level: str = "INFO"
    # Dump every frame on both sockets. The tool for "it connects but shows
    # nothing"; far too loud to leave on.
    trace_frames: bool = False
    # The meshtastic package's own logger, kept off our dial because it logs
    # every received frame at DEBUG.
    meshtastic_log_level: str = "WARNING"
    log_file: Path | None = None
    # Also log to the console. start.sh turns this off in the background, where
    # the rotating file is the log and the console is a redirect nobody rotates.
    log_console: bool = True
    log_max_bytes: int = 8_000_000
    log_backups: int = 3

    @property
    def replay_enabled(self) -> bool:
        return self.replay_mode != "none"


def config_file() -> Path | None:
    """The config file in effect, or None if unset or not present."""
    raw = os.environ.get("VNODE_CONFIG_FILE")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_file() else None


def _file_source(
    settings_cls: type[BaseSettings], path: Path
) -> PydanticBaseSettingsSource:
    """YAML or JSON, by suffix. Anything else is read as JSON, which is what
    /data/options.json is - the Supervisor writes it without an extension rule."""
    if path.suffix.lower() in (".yaml", ".yml"):
        return YamlConfigSettingsSource(settings_cls, yaml_file=path)
    return JsonConfigSettingsSource(settings_cls, json_file=path)


def load_settings(**overrides) -> Settings:
    return Settings(**{k: v for k, v in overrides.items() if v is not None})
