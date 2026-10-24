"""Configuration, read from the environment.

Only two values have no sensible default: where Switch is, and the token that
lets Relay register itself. Everything else is chosen so that a clone of this
repo runs against a stock local Switch without being edited.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


#: The repo root, two levels up from this file. `.env` is resolved against it
#: rather than the working directory, so `python -m relay` behaves the same
#: from the repo root and from inside conductor/.
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RELAY_",
        env_file=(REPO_ROOT / ".env", ".env"),
        extra="ignore",
    )

    # -- Switch ---------------------------------------------------------------

    switch_base_url: str = Field(
        default="http://localhost:8000",
        description=(
            "The agent bridge base URL -- the leg agents connect to, which is "
            "usually NOT the Gateway URL. GET /health against it is the "
            "correct probe: healthy means right, 401 means right host and "
            "wrong path."
        ),
    )
    registration_token: str = Field(
        default="",
        description=(
            "AGENT_REGISTRATION_TOKEN from the Switch server's .env. Used once, "
            "to register; rejected everywhere else. The Gateway user it "
            "identifies becomes Relay's owner, and Relay's permissions are "
            "inherited from them."
        ),
    )
    agent_name: str = Field(
        default="relay",
        description="Relay's name in Switch. This is what people and agents @.",
    )
    register_overwrite: bool = False

    # -- Rooms ----------------------------------------------------------------

    desk_room_name: str = Field(
        default="relay-desk",
        description=(
            "The room Relay treats as the desk: where requests arrive and the "
            "pipeline is reported. Resolved by name at startup via list_rooms, "
            "so the id never has to be configured."
        ),
    )
    duty_role: str = Field(
        default="duty",
        description=(
            "The room role Relay escalates to. Addressing a role rather than a "
            "name is what makes the workflow survive people rotating through "
            "it -- whoever holds it this week gets the page."
        ),
    )

    # -- Timing ---------------------------------------------------------------

    stall_minutes: int = Field(
        default=20,
        description="Silence on an open case before Relay nudges its owner.",
    )
    tick_seconds: int = Field(
        default=30, description="How often clocks are evaluated."
    )

    # -- Board ----------------------------------------------------------------

    api_host: str = "0.0.0.0"
    api_port: int = 8787
    state_dir: Path = Field(
        default=REPO_ROOT / ".relay",
        description=(
            "Where the case store and Relay's credentials live. Anchored to the "
            "repo rather than the working directory: the API key is returned "
            "once at registration, so a conductor started from a different "
            "folder must not come up as a stranger."
        ),
    )

    @property
    def db_path(self) -> Path:
        return self.state_dir / "relay.sqlite3"


def load() -> Settings:
    settings = Settings()
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    return settings
