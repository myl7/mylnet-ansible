import contextlib
import importlib.util
import io
import shlex
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "files" / "ensure_nft.py"
spec = importlib.util.spec_from_file_location("ensure_nft", MODULE_PATH)
assert spec is not None
ensure_nft = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = ensure_nft
spec.loader.exec_module(ensure_nft)


def nft_args(
    statement=None,
    *,
    family="inet",
    table="filter",
    chain="input_ipv4",
    comment: str | None = "ANSIBLE MANAGED RULE",
):
    if statement is None:
        statement = ["tcp", "dport", "8388", "accept"]

    args = ["add", "rule", family, table, chain, *statement]
    if comment is not None:
        args.extend(["comment", comment])
    return args


RULE_ARGS = nft_args()


def completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


class ParseArgsTest(unittest.TestCase):
    def test_parse_valid_rule(self):
        check, rule = ensure_nft.parse_args(RULE_ARGS)

        self.assertFalse(check)
        self.assertEqual(rule.family, "inet")
        self.assertEqual(rule.table, "filter")
        self.assertEqual(rule.chain, "input_ipv4")
        self.assertEqual(rule.statement.tokens, ["tcp", "dport", "8388", "accept"])
        self.assertEqual(
            rule.command_text,
            'add rule inet filter input_ipv4 tcp dport 8388 accept comment "ANSIBLE MANAGED RULE"',
        )

    def test_parse_check_and_separator(self):
        check, rule = ensure_nft.parse_args(["--check", "--", *RULE_ARGS])

        self.assertTrue(check)
        self.assertEqual(rule.statement.identity, ("tcp", "dport", "8388"))

    def test_parse_sport_and_drop(self):
        args = nft_args(["udp", "sport", "5353", "drop"])

        _, rule = ensure_nft.parse_args(args)

        self.assertEqual(rule.statement.tokens, ["udp", "sport", "5353", "drop"])
        self.assertEqual(rule.body, 'udp sport 5353 drop comment "ANSIBLE MANAGED RULE"')

    def test_parse_invalid_rules(self):
        cases = [
            nft_args(family="bridge"),
            nft_args(table="bad-name"),
            nft_args(["sctp", "dport", "8388", "accept"]),
            nft_args(["tcp", "xport", "8388", "accept"]),
            nft_args(["tcp", "dport", "0", "accept"]),
            nft_args(["tcp", "dport", "8388", "reject"]),
            nft_args(comment=None),
            nft_args(comment="OTHER"),
        ]

        for args in cases:
            with self.subTest(args=args):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as cm:
                        ensure_nft.parse_args(args)
                self.assertEqual(cm.exception.code, 2)


class ManagedRulesTest(unittest.TestCase):
    def setUp(self):
        _, self.rule = ensure_nft.parse_args(RULE_ARGS)

    def test_managed_rules_match_only_same_identity_and_comment(self):
        chain_rules = """
table inet filter {
  chain input_ipv4 {
    tcp dport 8388 accept # handle 10
    tcp dport 8388 accept comment "OTHER RULE" # handle 11
    tcp dport 8388 drop comment "ANSIBLE MANAGED RULE" # handle 12
    udp dport 8388 accept comment "ANSIBLE MANAGED RULE" # handle 13
    tcp sport 8388 accept comment "ANSIBLE MANAGED RULE" # handle 14
  }
}
"""

        entries = ensure_nft.managed_rules(chain_rules, self.rule)

        self.assertEqual([entry.handle for entry in entries], ["12"])
        self.assertEqual(entries[0].body, 'tcp dport 8388 drop comment "ANSIBLE MANAGED RULE"')

    def test_managed_rules_match_same_identity_with_extra_tokens(self):
        chain_rules = """
table inet filter {
  chain input_ipv4 {
    tcp dport 8388 counter drop comment "ANSIBLE MANAGED RULE" # handle 20
  }
}
"""

        entries = ensure_nft.managed_rules(chain_rules, self.rule)

        self.assertEqual([entry.handle for entry in entries], ["20"])
        self.assertEqual(
            entries[0].body,
            'tcp dport 8388 counter drop comment "ANSIBLE MANAGED RULE"',
        )

    def test_managed_rules_ignore_different_unsupported_identity(self):
        chain_rules = """
table inet filter {
  chain input_ipv4 {
    ip saddr 192.0.2.1 accept comment "ANSIBLE MANAGED RULE" # handle 21
  }
}
"""

        self.assertEqual(ensure_nft.managed_rules(chain_rules, self.rule), [])


class NftCommandTest(unittest.TestCase):
    def setUp(self):
        _, self.rule = ensure_nft.parse_args(RULE_ARGS)

    def test_list_chain_success(self):
        with mock.patch.object(ensure_nft, "run_nft", return_value=completed([], stdout="rules")) as run_nft:
            self.assertEqual(ensure_nft.list_chain(self.rule), "rules")

        run_nft.assert_called_once_with(["-s", "-a", "list", "chain", "inet", "filter", "input_ipv4"])

    def test_list_chain_failure_exits_one(self):
        with mock.patch.object(ensure_nft, "run_nft", return_value=completed([], returncode=1, stderr="missing\n")):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    ensure_nft.list_chain(self.rule)

        self.assertEqual(cm.exception.code, 1)

    def test_add_rule_argv(self):
        with mock.patch.object(ensure_nft, "run_nft", return_value=completed([])) as run_nft:
            ensure_nft.add_rule(self.rule)

        run_nft.assert_called_once_with(
            [
                "add",
                "rule",
                "inet",
                "filter",
                "input_ipv4",
                "tcp",
                "dport",
                "8388",
                "accept",
                "comment",
                '"ANSIBLE MANAGED RULE"',
            ]
        )

    def test_add_rule_comment_survives_nft_relexing(self):
        # nft concatenates its argv with spaces and re-lexes the result, so a
        # multi-word comment must carry literal quotes to stay one token.
        with mock.patch.object(ensure_nft, "run_nft", return_value=completed([])) as run_nft:
            ensure_nft.add_rule(self.rule)

        (argv,), _ = run_nft.call_args
        relexed = shlex.split(" ".join(argv))
        comment_index = relexed.index("comment")
        self.assertEqual(relexed[comment_index + 1 :], ["ANSIBLE MANAGED RULE"])

    def test_delete_rule_argv(self):
        with mock.patch.object(ensure_nft, "run_nft", return_value=completed([])) as run_nft:
            ensure_nft.delete_rule(self.rule, "42")

        run_nft.assert_called_once_with(["delete", "rule", "inet", "filter", "input_ipv4", "handle", "42"])


class MainTest(unittest.TestCase):
    def run_main(self, chain_rules, args=None):
        if args is None:
            args = RULE_ARGS
        stdout = io.StringIO()
        with (
            mock.patch.object(ensure_nft, "list_chain", return_value=chain_rules),
            mock.patch.object(ensure_nft, "add_rule") as add_rule,
            mock.patch.object(ensure_nft, "delete_rule") as delete_rule,
            contextlib.redirect_stdout(stdout),
        ):
            rc = ensure_nft.main(args)

        return rc, stdout.getvalue().splitlines(), add_rule, delete_rule

    def test_exact_rule_is_unchanged(self):
        chain_rules = """
table inet filter {
  chain input_ipv4 {
    tcp dport 8388 accept comment "ANSIBLE MANAGED RULE" # handle 10
  }
}
"""

        rc, lines, add_rule, delete_rule = self.run_main(chain_rules)

        self.assertEqual(rc, 0)
        self.assertIn("changed=false", lines)
        add_rule.assert_not_called()
        delete_rule.assert_not_called()

    def test_missing_managed_rule_adds_without_deleting_unmanaged(self):
        chain_rules = """
table inet filter {
  chain input_ipv4 {
    tcp dport 8388 accept # handle 11
    tcp dport 8388 accept comment "OTHER RULE" # handle 12
  }
}
"""

        rc, lines, add_rule, delete_rule = self.run_main(chain_rules)

        self.assertEqual(rc, 0)
        self.assertIn("changed=true", lines)
        add_rule.assert_called_once()
        delete_rule.assert_not_called()

    def test_stale_managed_rule_adds_then_deletes_managed_handle(self):
        chain_rules = """
table inet filter {
  chain input_ipv4 {
    tcp dport 8388 drop comment "ANSIBLE MANAGED RULE" # handle 13
  }
}
"""

        rc, lines, add_rule, delete_rule = self.run_main(chain_rules)

        self.assertEqual(rc, 0)
        self.assertIn("changed=true", lines)
        add_rule.assert_called_once()
        delete_rule.assert_called_once_with(mock.ANY, "13")

    def test_check_mode_reports_changed_without_mutation(self):
        rc, lines, add_rule, delete_rule = self.run_main("", ["--check", "--", *RULE_ARGS])

        self.assertEqual(rc, 0)
        self.assertIn("changed=true", lines)
        add_rule.assert_not_called()
        delete_rule.assert_not_called()

    def test_add_failure_does_not_delete_old_managed_rule(self):
        chain_rules = """
table inet filter {
  chain input_ipv4 {
    tcp dport 8388 drop comment "ANSIBLE MANAGED RULE" # handle 13
  }
}
"""
        stdout = io.StringIO()
        with (
            mock.patch.object(ensure_nft, "list_chain", return_value=chain_rules),
            mock.patch.object(ensure_nft, "add_rule", side_effect=SystemExit(1)) as add_rule,
            mock.patch.object(ensure_nft, "delete_rule") as delete_rule,
            contextlib.redirect_stdout(stdout),
        ):
            with self.assertRaises(SystemExit) as cm:
                ensure_nft.main(RULE_ARGS)

        self.assertEqual(cm.exception.code, 1)
        add_rule.assert_called_once()
        delete_rule.assert_not_called()


if __name__ == "__main__":
    unittest.main()
