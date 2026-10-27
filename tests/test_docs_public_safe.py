"""Keep docs/ publishable.

The docs directory is copied to a public repository. Machine facts
(hostnames, addresses, ports, account names) belong to the playbooks and
inventories, so this scan fails closed on identifiers that must never
appear in a doc. A hit means: move the fact to the playbook, or replace
it with a role or placeholder. Extend BANNED whenever a new identifier
must never leak.
"""

import unittest
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"

# Lowercase; matched case-insensitively. Hostnames and addresses of the
# fleet and the lab machines, the tailnet's DNS name, the SSH port, and
# account or node names that identify real machines.
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
            "docs/ is published; move machine facts to the playbooks: "
            + "; ".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
