"""Keep docs/ inside the repo's public disclosure.

The repository itself is public, so hostnames, domains, addresses, ports
and account names that tracked files already carry are fine to repeat in
a doc. This scan fails closed only on identifiers the repo does NOT
expose. A hit means: move the fact out of docs (or replace it with a
placeholder). Extend BANNED when a new non-repo identifier must never
leak; do not add tokens that inventories or playbooks already publish.
"""

import unittest
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"

# Lowercase; matched case-insensitively. Tripwires for the two banned
# categories: machine-internal state of the cityu GPU servers that the
# repo does not disclose (pre-existing config defects, outage history),
# and credential relationships. Lab usernames beyond the ones hosts.toml
# already names fall under the first category.
BANNED = ()


class DocsPublicSafe(unittest.TestCase):
    def test_no_banned_identifiers_in_docs(self):
        violations = []
        for path in sorted(DOCS.glob("*.md")):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                for token in BANNED:
                    if token in line.lower():
                        violations.append(f"{path.name}:{number} {token}")
        self.assertEqual(
            [],
            violations,
            "docs/ must not carry facts the repo does not expose: "
            + "; ".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
