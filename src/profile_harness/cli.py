"""Command-line interface for profile initialization and registration."""

from __future__ import annotations

import argparse
from pathlib import Path

from .config import init_profile, register_repo


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="profile-harness")
    commands = parser.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser("init", help="initialize a profile")
    initialize.add_argument("profile_root", type=Path)
    initialize.add_argument("--name", required=True)

    register = commands.add_parser(
        "register-repo", help="register a repository below the profile projects folder"
    )
    register.add_argument("profile_root", type=Path)
    register.add_argument("name")
    register.add_argument("path", type=Path)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "init":
            init_profile(arguments.profile_root, arguments.name)
        elif arguments.command == "register-repo":
            register_repo(arguments.profile_root, arguments.name, arguments.path)
    except (OSError, ValueError) as error:
        parser.error(str(error))
