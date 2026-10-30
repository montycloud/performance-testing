"""MontyCloud API environments, shared by every tool's --env option."""

from __future__ import annotations

ENVIRONMENTS: dict[str, str] = {
    "dev1": "https://dev-api.montycloud.com",
    "dev-eu": "https://dev-eu-api.montycloud.com",
    "eu": "https://eu-api.montycloud.com",
    "int1": "https://int1-api.montycloud.com",
    "stg1": "https://stg1-api.montycloud.com",
    "prd01": "https://api.montycloud.com",
}

# Short names used by older configs and the Day2SDK --env flag.
ALIASES: dict[str, str] = {"dev": "dev1", "stg": "stg1", "prod": "prd01"}

PRODUCTION: frozenset[str] = frozenset({"prd01"})


def canonical(env: str) -> str:
    name = env.strip().lower()
    name = ALIASES.get(name, name)
    if name not in ENVIRONMENTS:
        known = ", ".join(sorted([*ENVIRONMENTS, *ALIASES]))
        raise ValueError(f"Unknown environment {env!r}. Known: {known}.")
    return name


def base_url(env: str) -> str:
    """API root for *env*, e.g. base_url("stg") -> "https://stg1-api.montycloud.com"."""
    return ENVIRONMENTS[canonical(env)]


def is_production(env: str) -> bool:
    return canonical(env) in PRODUCTION
