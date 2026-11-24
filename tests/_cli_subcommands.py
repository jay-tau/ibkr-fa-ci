"""The CLI parser's subcommand map, name to subparser, shared by the argparse
error tests and the Robinhood tests that check both FA commands accept each
flag their refusals and actions name."""

from __future__ import annotations

import argparse


def subcommands(parser: argparse.ArgumentParser):
    [commands] = [
        action._name_parser_map
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    return commands
