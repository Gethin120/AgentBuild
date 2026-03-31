from __future__ import annotations

import unittest

from app.chat_cli import parse_slash_command, resolve_select_ref


class ChatCliTests(unittest.TestCase):
    def test_parse_slash_command(self) -> None:
        command, argument = parse_slash_command("/select 2")

        self.assertEqual(command, "select")
        self.assertEqual(argument, "2")

    def test_resolve_select_ref_numeric(self) -> None:
        self.assertEqual(resolve_select_ref("1"), "recommended")
        self.assertEqual(resolve_select_ref("2"), "alternative_1")


if __name__ == "__main__":
    unittest.main()
