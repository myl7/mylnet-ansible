"""Jinja2 filters for the proxy playbook."""

from typing import Any

from ansible.errors import AnsibleFilterError  # type: ignore[import-untyped]


def move_first(items: list[Any], value: Any) -> list[Any]:
    """Return ``items`` reordered so ``value`` comes first.

    Raise ``AnsibleFilterError`` when ``value`` is absent, because a missing
    entry would render a proxy group that references an undefined proxy.
    """
    if value not in items:
        raise AnsibleFilterError(f"move_first: {value!r} not in {items!r}")
    return [value] + [item for item in items if item != value]


class FilterModule:
    """Expose the custom filters to Ansible."""

    def filters(self) -> dict[str, Any]:
        return {"move_first": move_first}
