"""Operational commands for platform administration.

Commands for managing operational concerns like dead letter events,
system health, and other administrative tasks.
"""

import typer

from pragma_cli.commands import dead_letter


app = typer.Typer(help="Operational commands for platform administration")

app.add_typer(dead_letter.app, name="dead-letter")
