"""
Stack naming convention helpers.

All framework resource stacks follow the pattern:
  {prefix}-{short}-{start:06d}-{end:06d}
  e.g.  mc-perftest-sns-000001-000200

The Lambda roles prerequisite stack is named:
  {prefix}-lambda-roles
"""

from cfn.constants import SHORT_TO_RESOURCE


def make_stack_name(prefix: str, short: str, start: int, end: int) -> str:
    return f"{prefix}-{short}-{start:06d}-{end:06d}"


def parse_stack_name(prefix: str, name: str) -> dict | None:
    """Return {name, resource, short, start, end} or None if not a framework stack."""
    if not name.startswith(f"{prefix}-"):
        return None
    rest = name[len(prefix) + 1:]
    parts = rest.rsplit("-", 2)
    if len(parts) != 3:
        return None
    short, start_str, end_str = parts
    if short not in SHORT_TO_RESOURCE:
        return None
    try:
        return {
            "name": name,
            "short": short,
            "resource": SHORT_TO_RESOURCE[short],
            "start": int(start_str),
            "end": int(end_str),
        }
    except ValueError:
        return None
