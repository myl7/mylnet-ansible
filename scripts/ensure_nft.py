#!/usr/bin/env python3
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from typing import Never


MANAGED_COMMENT = "ANSIBLE MANAGED RULE"
SUPPORTED_VERDICTS = {"accept", "drop"}

USAGE = f"""Usage:
    ensure_nft.py [--check] add rule FAMILY TABLE CHAIN MATCH VERDICT comment "{MANAGED_COMMENT}"

Supported MATCH:
    tcp|udp sport|dport PORT

Supported VERDICT:
    accept|drop
"""


@dataclass(frozen=True)
class RuleStatement:
    proto: str
    port_field: str
    port: str
    verdict: str

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.proto, self.port_field, self.port)

    @property
    def tokens(self) -> list[str]:
        return [self.proto, self.port_field, self.port, self.verdict]

    @property
    def text(self) -> str:
        return " ".join(self.tokens)


@dataclass(frozen=True)
class Rule:
    family: str
    table: str
    chain: str
    statement: RuleStatement
    comment: str

    @property
    def body(self) -> str:
        return f'{self.statement.text} comment "{self.comment}"'

    @property
    def command_text(self) -> str:
        return f"add rule {self.family} {self.table} {self.chain} {self.body}"


@dataclass(frozen=True)
class ExistingRule:
    handle: str
    body: str
    identity: tuple[str, str, str]


def fail(message: str) -> Never:
    print(message, file=sys.stderr)
    print(USAGE, file=sys.stderr, end="")
    raise SystemExit(2)


def validate_name(kind: str, value: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        fail(f"Unsupported {kind} name: {value}")


def valid_port(port: str) -> bool:
    return port.isdigit() and 1 <= int(port) <= 65535


def build_statement(tokens: list[str]) -> tuple[RuleStatement | None, str]:
    if len(tokens) < 4:
        return None, "Missing rule statement."

    if len(tokens) != 4:
        return None, f"Unsupported rule statement: {' '.join(tokens)}"

    proto, port_field, port, verdict = tokens
    if proto not in {"tcp", "udp"}:
        return None, "Only tcp and udp matches are supported."
    if port_field not in {"sport", "dport"}:
        return None, "Only sport and dport matches are supported."
    if not valid_port(port):
        return None, f"Invalid port: {port}"
    if verdict not in SUPPORTED_VERDICTS:
        return None, f"Unsupported verdict: {verdict}"

    return RuleStatement(
        proto=proto,
        port_field=port_field,
        port=port,
        verdict=verdict,
    ), ""


def parse_statement(tokens: list[str]) -> RuleStatement:
    statement, error = build_statement(tokens)
    if statement is None:
        fail(error)
    return statement


def split_comment(tokens: list[str]) -> tuple[list[str], str] | None:
    try:
        comment_index = tokens.index("comment")
    except ValueError:
        return None

    statement_tokens = tokens[:comment_index]
    comment_tokens = tokens[comment_index + 1 :]
    if not comment_tokens:
        return None

    return statement_tokens, " ".join(comment_tokens)


def parse_managed_comment(tokens: list[str]) -> tuple[list[str], str]:
    parsed = split_comment(tokens)
    if parsed is None:
        fail(f'Managed nft rules must include comment "{MANAGED_COMMENT}".')

    statement_tokens, comment = parsed
    if comment != MANAGED_COMMENT:
        fail(f"Unsupported comment: {comment}")

    return statement_tokens, comment


def parse_args(argv: list[str]) -> tuple[bool, Rule]:
    check = False

    while argv:
        if argv[0] == "--check":
            check = True
            argv = argv[1:]
        elif argv[0] == "--":
            argv = argv[1:]
            break
        elif argv[0].startswith("--"):
            fail(f"Unsupported option: {argv[0]}")
        else:
            break

    if len(argv) < 11:
        fail(f'Managed nft rules must include comment "{MANAGED_COMMENT}".')

    if argv[0] != "add" or argv[1] != "rule":
        fail("Only 'add rule' is supported.")

    family, table, chain = argv[2:5]
    if family not in {"inet", "ip", "ip6"}:
        fail(f"Unsupported family: {family}")
    validate_name("table", table)
    validate_name("chain", chain)

    statement_tokens, comment = parse_managed_comment(argv[5:])
    return check, Rule(
        family=family,
        table=table,
        chain=chain,
        statement=parse_statement(statement_tokens),
        comment=comment,
    )


def run_nft(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["nft", *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def list_chain(rule: Rule) -> str:
    result = run_nft(["-s", "-a", "list", "chain", rule.family, rule.table, rule.chain])
    if result.returncode == 0:
        return result.stdout

    print(
        f"Failed to read nftables chain {rule.family} {rule.table} {rule.chain}.",
        file=sys.stderr,
    )
    print("Run the base nftables setup first so the target ruleset exists.", file=sys.stderr)
    print(result.stderr or result.stdout, file=sys.stderr, end="")
    raise SystemExit(1)


def parse_existing_identity(body: str) -> tuple[str, str, str] | None:
    try:
        tokens = shlex.split(body)
    except ValueError:
        return None

    parsed = split_comment(tokens)
    if parsed is None:
        return None

    statement_tokens, comment = parsed
    if comment != MANAGED_COMMENT:
        return None

    if len(statement_tokens) < 3:
        return None

    proto, port_field, port = statement_tokens[:3]
    if proto not in {"tcp", "udp"}:
        return None
    if port_field not in {"sport", "dport"}:
        return None
    if not valid_port(port):
        return None

    return (proto, port_field, port)


def managed_rules(chain_rules: str, rule: Rule) -> list[ExistingRule]:
    entries: list[ExistingRule] = []
    handle_re = re.compile(r"^\s*(?P<body>.*?)\s+# handle (?P<handle>\d+)\s*$")

    for raw_line in chain_rules.splitlines():
        match = handle_re.match(raw_line)
        if not match:
            continue

        body = match.group("body").strip()
        identity = parse_existing_identity(body)
        if identity is None:
            continue
        if identity != rule.statement.identity:
            continue

        entries.append(ExistingRule(handle=match.group("handle"), body=body, identity=identity))

    return entries


def add_rule(rule: Rule) -> None:
    result = run_nft(
        [
            "add",
            "rule",
            rule.family,
            rule.table,
            rule.chain,
            *rule.statement.tokens,
            "comment",
            f'"{rule.comment}"',
        ]
    )
    if result.returncode == 0:
        return

    print("Failed to add nftables rule.", file=sys.stderr)
    print(result.stderr or result.stdout, file=sys.stderr, end="")
    raise SystemExit(1)


def delete_rule(rule: Rule, handle: str) -> None:
    result = run_nft(["delete", "rule", rule.family, rule.table, rule.chain, "handle", handle])
    if result.returncode == 0:
        return

    print(f"Failed to delete managed nftables rule handle {handle}.", file=sys.stderr)
    print(result.stderr or result.stdout, file=sys.stderr, end="")
    raise SystemExit(1)


def main(argv: list[str]) -> int:
    check, rule = parse_args(argv)
    entries = managed_rules(list_chain(rule), rule)
    exact_count = sum(entry.body == rule.body for entry in entries)

    print(f"rule={rule.command_text}")

    if len(entries) == 1 and exact_count == 1:
        print("changed=false")
        return 0

    print("changed=true")
    if check:
        return 0

    add_rule(rule)
    for entry in entries:
        delete_rule(rule, entry.handle)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
