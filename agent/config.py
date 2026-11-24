"""Explicit, side-effect-free configuration loading."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr

DAILY_LIMIT = 15
SEND_TIMEZONE = "Asia/Kolkata"
MIN_SEND_DELAY_SECONDS = 60
MAX_SEND_DELAY_SECONDS = 180
SUBJECT_TEMPLATE = "A quick idea for {course_or_business}"
INITIAL_TEMPLATE = """Hi {first_name},

{hook_line}

I build AI assistants for course creators: a small chat box on your site, trained only on your own FAQ and course pages, that can answer questions like "{q1}" or "{q2}". Anything it can't answer goes back to you.

If you're open to it, I'll build a free demo trained on your public FAQ so you can test it yourself. No cost, no obligation.

Would you like me to make the demo?

Kartik Khandelwal
AI developer
https://my-portfolio-azure-nine-26.vercel.app

{postal_address}

P.S. If this isn't useful, just reply "no thanks" and I won't follow up.
"""

UPGRADE_TEMPLATE = INITIAL_TEMPLATE.replace(
    "I build AI assistants for course creators: a small chat box on your site,",
    "For an assistant upgrade, I build a small chat box for your site,",
)


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    database_path: Path = Path("data/outreach.sqlite3")
    dry_run: bool = True
    paused: bool = True
    daily_limit: int = Field(default=DAILY_LIMIT, ge=1, le=15)
    owner_test_addresses: str = ""
    gmail_account: str = ""
    gmail_client_file: Path = Path("credentials.json")
    postal_address: str = ""
    postal_address_confirmed: bool = False
    dns_timeout_seconds: float = Field(default=5, gt=0, le=60)
    groq_api_key: SecretStr | None = None
    groq_model: str = Field(default="openai/gpt-oss-120b", min_length=1)
    search_api_key: SecretStr | None = None
    search_provider: Literal["tavily", "serper"] = "tavily"
    search_excluded_domains: str = (
        "mapquest.com,cbinsights.com,onlinejobs.ph,tavily.com,google.com,bing.com,yahoo.com"
    )
    search_excluded_hosts: str = (
        "teachable.com,thinkific.com,ruzuku.com,kajabi.com,podia.com,learnworlds.com"
    )
    search_results_per_query: int = Field(default=10, ge=1, le=20)


def load_settings(
    env_file: str | Path | None = ".env", environ: Mapping[str, str] | None = None
) -> Settings:
    """Process values override .env; never modify os.environ or load at import time."""
    values = dict(dotenv_values(env_file, interpolate=False)) if env_file else {}
    values.update(os.environ if environ is None else environ)
    fields = {}
    for name in Settings.model_fields:
        value = values.get(name.upper())
        if value is not None:
            fields[name] = value
    return Settings.model_validate(fields)
