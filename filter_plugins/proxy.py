"""Jinja2 filters for the proxy playbook."""

from typing import Any

from ansible.errors import AnsibleFilterError  # type: ignore[import-untyped]


def move_first(profiles: list[dict[str, Any]], value: str, attribute: str = "host") -> list[dict[str, Any]]:
    """Return ``profiles`` reordered so the entry whose ``attribute`` equals ``value`` comes first.

    Raise ``AnsibleFilterError`` when nothing matches, because a missing entry
    would render a proxy group that references an undefined proxy.
    """
    matched = [profile for profile in profiles if profile.get(attribute) == value]
    if not matched:
        raise AnsibleFilterError(f"move_first: no profile with {attribute}={value!r}")
    rest = [profile for profile in profiles if profile.get(attribute) != value]
    return matched + rest


class FilterModule:
    """Expose the custom filters to Ansible."""

    def filters(self) -> dict[str, Any]:
        return {"move_first": move_first}
