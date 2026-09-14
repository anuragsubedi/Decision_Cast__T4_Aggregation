"""Typed wrapper over the environment.

Values come from, highest precedence first:

    real environment variables  >  .env at the project root  >  defaults below

Each line is one setting: its name, its type, and its default. That is the
whole file. The type is what turns ``TIER4_POLL_INTERVAL_SECONDS=5`` into the
float ``5.0`` and ``TIER4_AUTOSTART_POLLER=1`` into ``True`` instead of the
strings ``"5"`` and ``"1"``.

Two rules that follow from how this works:

* **Credentials have no defaults.** They are required, so a misconfigured
  deployment fails loudly at startup instead of silently trying a dev password.
  Nothing secret is committed in this file.
* **Read settings through ``settings``, never ``os.environ``.**
  pydantic-settings loads ``.env`` into the model without exporting it to the
  process environment, so an ``os.environ`` lookup silently ignores ``.env``.

See docs/RUNNING.md for the deployment story.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchored to the project root, not the cwd, so the CLI and the debugger behave
# identically wherever they are launched from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_PROJECT_ROOT / ".env", env_prefix="TIER4_", extra="ignore"
    )

    # --- backend (tier 2) --------------------------------------------------
    # Scheme must match the port: the backend serves TLS on :8855 and plain
    # HTTP on :8844. See docs/RUNNING.md §1.
    be_base_url: str = "https://localhost:8855"
    be_verify_tls: bool = False          # dev cert is self-signed; ON in prod
    be_email: str                        # required
    be_password: SecretStr               # required; auto-redacted when dumped
    request_timeout_seconds: float = 30.0

    # --- worker loop -------------------------------------------------------
    aggregator_name: str = "tier4-datascience"
    poll_interval_seconds: float = 5.0
    max_iterations: int = 0              # 0 = run until stopped
    claim_unknown_methods: bool = False  # leave unknown methods for other workers

    # --- FastAPI app -------------------------------------------------------
    autostart_poller: bool = False       # poll in the background when serving

    def public_summary(self) -> dict[str, Any]:
        """Effective config, safe to log or return over HTTP.

        ``mode="json"`` is what renders ``SecretStr`` as ``**********``, so
        redaction is structural rather than a list of field names to maintain.
        """
        return self.model_dump(mode="json")


settings = Settings()
