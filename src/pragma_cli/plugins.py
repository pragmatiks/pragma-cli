"""Discover and mount Typer subcommands registered via pragma.commands entry-point group."""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

import typer


logger = logging.getLogger(__name__)

PLUGIN_GROUP = "pragma.commands"


def load_plugins(app: typer.Typer) -> None:
    """Mount every Typer subcommand registered for the pragma.commands group.

    Each discovered entry point loads to a Typer instance and is mounted as
    a top-level subcommand under its entry-point name. Broken plugins
    (import failures, non-Typer values) log a warning and are skipped, so the
    host CLI never crashes because a plugin is malformed.

    Args:
        app: Root Typer application to receive plugin subcommands.
    """
    existing_names = {group.name for group in getattr(app, "registered_groups", []) if group.name is not None}
    existing_names.update(
        command.name for command in getattr(app, "registered_commands", []) if command.name is not None
    )

    for entry in entry_points(group=PLUGIN_GROUP):
        if entry.name in existing_names:
            logger.warning("Plugin %r conflicts with an already-registered command, skipping", entry.name)
            continue

        try:
            subcommand = entry.load()
        except Exception:
            logger.warning("Failed to load pragma plugin %r", entry.name, exc_info=True)
            continue

        if not isinstance(subcommand, typer.Typer):
            logger.warning(
                "Plugin %r resolved to %s, expected typer.Typer",
                entry.name,
                type(subcommand).__name__,
            )
            continue

        app.add_typer(subcommand, name=entry.name)
        existing_names.add(entry.name)
