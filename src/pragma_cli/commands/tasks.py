"""Read-only commands for inspecting the agent task board.

Tasks are read-only on the CLI by design — the web is the write
surface. The CLI surfaces the board summary, task list and detail,
comments, activity timeline, paginated mutation log, net-delta graph
diff, and direct subtasks for inspection and audit.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import httpx
import typer
from pragma_sdk import (
    BoardSummary,
    GraphDiff,
    Task,
    TaskActivityEntry,
    TaskComment,
    TaskMutationPage,
    TaskStatus,
)
from rich.console import Console
from rich.markup import escape as rich_escape
from rich.table import Table

from pragma_cli import get_client
from pragma_cli.helpers import OutputFormat, output_data


app = typer.Typer(help="Inspect agent tasks (read-only)")

console = Console()


_PRIORITY_LABELS: dict[int, str] = {
    1: "urgent",
    2: "high",
    3: "normal",
    4: "low",
}

_STATUS_COLORS: dict[str, str] = {
    "backlog": "dim",
    "assigned": "yellow",
    "running": "cyan",
    "review": "magenta",
    "done": "green",
}


def _format_priority(priority: int | None) -> str:
    """Render a numeric task priority as ``"<n> (<label>)"``.

    Args:
        priority: Numeric priority (1-4) from the task model.

    Returns:
        Human-readable priority string, or empty string when ``None``.
    """
    if priority is None:
        return ""

    label = _PRIORITY_LABELS.get(priority, "unknown")
    return f"{priority} ({label})"


def _format_status(status: str | TaskStatus | None) -> str:
    """Render a task status with its conventional Rich color.

    Args:
        status: Status value as plain string or enum.

    Returns:
        Rich-markup colored status string.
    """
    if status is None:
        return ""

    value = status.value if isinstance(status, TaskStatus) else str(status)
    color = _STATUS_COLORS.get(value, "white")
    return f"[{color}]{value}[/{color}]"


def _format_timestamp(value: Any) -> str:
    """Render a timestamp value as an ISO string for tables.

    Args:
        value: Datetime-like value, ISO string, or ``None``.

    Returns:
        ISO-formatted string suitable for display, or empty string.
    """
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _safe(value: str | None) -> str:
    """Escape Rich markup metacharacters in untrusted server strings.

    Args:
        value: Untrusted string returned by the API.

    Returns:
        String safe to embed in Rich-formatted output.
    """
    if value is None:
        return ""
    return rich_escape(value)


def _print_board(payload: list[dict[str, Any]] | dict[str, Any]) -> None:
    """Render the board summary as a counts-per-status table.

    Args:
        payload: BoardSummary serialized payload (single dict).
    """
    summary = payload if isinstance(payload, dict) else payload[0]

    table = Table(show_header=True, header_style="bold")
    table.add_column("Status")
    table.add_column("Count", justify="right")

    counts: dict[str, int] = summary.get("counts", {})
    for status, count in counts.items():
        color = _STATUS_COLORS.get(status, "white")
        table.add_row(f"[{color}]{status}[/{color}]", str(count))

    table.add_row("[bold]total[/bold]", f"[bold]{summary.get('total', 0)}[/bold]")
    console.print(table)


def _print_task_list(payload: list[dict[str, Any]]) -> None:
    """Render a list of tasks as a summary table.

    Args:
        payload: List of Task payloads.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID")
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("Priority")
    table.add_column("Assignee")
    table.add_column("Updated")

    for task in payload:
        table.add_row(
            _safe(task.get("id")),
            _safe(task.get("title")),
            _format_status(task.get("status")),
            _format_priority(task.get("priority")),
            _safe(_format_assignee_from_dict(task)),
            _format_timestamp(task.get("updated_at")),
        )

    console.print(table)


def _format_assignee_from_dict(task: dict[str, Any]) -> str:
    """Render assignee summary from a serialized task dict.

    Args:
        task: Task payload as a dict (post ``model_dump``).

    Returns:
        ``"<kind>:<id>"`` short label, or ``"-"`` when unassigned.
    """
    if instance := task.get("assigned_to_instance_id"):
        return f"agent:{instance}"
    if user := task.get("assigned_to_user_id"):
        return f"user:{user}"
    if type_id := task.get("assigned_to_type_id"):
        return f"type:{type_id}"
    return "-"


def _print_task_detail(payload: list[dict[str, Any]]) -> None:
    """Render a single task as key-value rows.

    Args:
        payload: Single-item list containing the task payload.
    """
    task = payload[0]

    table = Table(show_header=False, box=None)
    table.add_column("Field", style="bold")
    table.add_column("Value")

    table.add_row("ID", _safe(task.get("id")))
    table.add_row("Title", _safe(task.get("title")))
    table.add_row("Status", _format_status(task.get("status")))
    table.add_row("Priority", _format_priority(task.get("priority")))
    table.add_row("Source", _safe(str(task.get("source") or "")))
    table.add_row("Assignee", _safe(_format_assignee_from_dict(task)))
    table.add_row("Correlation Bucket", _safe(task.get("correlation_bucket_id") or "-"))
    table.add_row("Created By", _safe(task.get("created_by") or "-"))
    table.add_row("Created", _format_timestamp(task.get("created_at")))
    table.add_row("Updated", _format_timestamp(task.get("updated_at")))

    console.print(table)

    description = task.get("description")
    if description:
        console.print()
        console.print("[bold]Description:[/bold]")
        console.print(_safe(description))


def _print_comments(payload: list[dict[str, Any]]) -> None:
    """Render comments as an author/timestamp/body table.

    Args:
        payload: List of TaskComment payloads.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("Author")
    table.add_column("When")
    table.add_column("Edited")
    table.add_column("Body")

    for comment in payload:
        table.add_row(
            _safe(_format_comment_author(comment)),
            _format_timestamp(comment.get("created_at")),
            "yes" if comment.get("edited") else "no",
            _safe(comment.get("body")),
        )

    console.print(table)


def _format_comment_author(comment: dict[str, Any]) -> str:
    """Render the author of a comment as a short ``kind:id`` label.

    Args:
        comment: TaskComment payload as a dict.

    Returns:
        ``"user:<id>"`` for user comments, ``"agent:<instance>"`` for
        agent comments, or ``"unknown"`` when the author cannot be
        determined.
    """
    author_type = comment.get("author_type")

    if author_type == "user" and (user_id := comment.get("author_user_id")):
        return f"user:{user_id}"

    if author_type == "agent":
        if instance_id := comment.get("author_instance_id"):
            return f"agent:{instance_id}"
        if type_id := comment.get("author_agent_type_id"):
            return f"type:{type_id}"

    return "unknown"


def _print_activity(payload: list[dict[str, Any]]) -> None:
    """Render the activity timeline as a kind/when/summary table.

    Args:
        payload: List of TaskActivityEntry payloads.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("When")
    table.add_column("Kind")
    table.add_column("Summary")

    for entry in payload:
        table.add_row(
            _format_timestamp(entry.get("timestamp")),
            _safe(str(entry.get("kind") or "")),
            _safe(_summarize_activity(entry)),
        )

    console.print(table)


def _summarize_activity(entry: dict[str, Any]) -> str:
    """Compose a one-line summary for an activity entry.

    Each entry kind has its own relevant fields. We only surface the
    ones relevant to ``kind`` and skip the rest so the timeline column
    stays terse.

    Args:
        entry: TaskActivityEntry payload as a dict.

    Returns:
        Human-readable summary line.
    """
    kind = entry.get("kind")

    if kind == "transition":
        return f"{entry.get('from_status') or '?'} -> {entry.get('to_status') or '?'}"

    if kind == "assignment":
        table = entry.get("assignee_table") or "?"
        assignee_id = entry.get("assignee_id") or "?"
        return f"assigned {table}:{assignee_id}"

    if kind == "comment":
        return f"comment {entry.get('comment_id') or ''}".strip()

    if kind == "agent_started":
        return f"instance {entry.get('instance_id') or ''}".strip()

    if kind == "mutation":
        operation = entry.get("operation") or "?"
        resource_table = entry.get("resource_table") or ""
        resource_id = entry.get("resource_id") or ""
        fields = entry.get("fields_changed") or []
        target = f"{resource_table}:{resource_id}" if resource_table else resource_id
        suffix = f" [{', '.join(fields)}]" if fields else ""
        return f"{operation} {target}{suffix}"

    return ""


def _print_mutations(payload: list[dict[str, Any]] | dict[str, Any]) -> None:
    """Render the mutation log page as a per-mutation table.

    Args:
        payload: TaskMutationPage payload (dict with ``items`` and
            ``next_cursor``).
    """
    page = payload if isinstance(payload, dict) else payload[0]
    items: list[dict[str, Any]] = page.get("items", [])

    if not items:
        console.print("[dim]No mutations found.[/dim]")
        _print_next_cursor(page.get("next_cursor"))
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("When")
    table.add_column("Op")
    table.add_column("Resource")
    table.add_column("Fields changed")
    table.add_column("Actor")

    for mutation in items:
        table.add_row(
            _format_timestamp(mutation.get("timestamp")),
            _safe(str(mutation.get("operation") or "")),
            _safe(_format_mutation_target(mutation)),
            _safe(", ".join(mutation.get("fields_changed") or [])),
            _safe(_format_mutation_actor(mutation)),
        )

    console.print(table)
    _print_next_cursor(page.get("next_cursor"))


def _format_mutation_target(mutation: dict[str, Any]) -> str:
    """Compose the ``"<table>:<id>"`` label for a mutation row.

    Args:
        mutation: ResourceMutation payload as a dict.

    Returns:
        Combined target label, or just the resource id when the table
        is missing.
    """
    table = mutation.get("resource_table") or ""
    resource_id = mutation.get("resource_id") or ""

    if table:
        return f"{table}:{resource_id}"

    return resource_id


def _format_mutation_actor(mutation: dict[str, Any]) -> str:
    """Compose the ``"<actor_type>:<actor_id>"`` label for a mutation row.

    Args:
        mutation: ResourceMutation payload as a dict.

    Returns:
        Actor label, or just the type when the id is missing.
    """
    actor_type = mutation.get("actor_type") or ""
    actor_id = mutation.get("actor_id") or ""

    if actor_id:
        return f"{actor_type}:{actor_id}"

    return actor_type


def _print_next_cursor(cursor: str | None) -> None:
    """Print a hint with the pagination cursor for the next page.

    Args:
        cursor: Composite cursor returned by the API, or ``None``.
    """
    if cursor:
        console.print(f"[dim]Next page cursor: {cursor}[/dim]")


def _print_graph_diff(payload: list[dict[str, Any]] | dict[str, Any]) -> None:
    """Render the per-resource net delta with a truncation banner.

    Args:
        payload: GraphDiff payload (single dict).
    """
    diff = payload if isinstance(payload, dict) else payload[0]
    resources: list[dict[str, Any]] = diff.get("resources", [])

    if diff.get("truncated"):
        scanned = diff.get("total_mutations_scanned") or diff.get("total_mutations") or 0
        console.print(f"[yellow]Showing partial view ({scanned} of N changes) — open mutation log[/yellow]")
        console.print(f"[dim]Hint: pragma tasks mutations {diff.get('task_id')}[/dim]")
        console.print()

    if not resources:
        console.print("[dim]No resource changes for this task.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Resource")
    table.add_column("Net op")
    table.add_column("Fields changed")
    table.add_column("Mutations", justify="right")

    for resource in resources:
        target = f"{resource.get('resource_table') or ''}:{resource.get('resource_id') or ''}"
        table.add_row(
            _safe(target),
            _safe(str(resource.get("net_operation") or "")),
            _safe(", ".join(resource.get("fields_changed") or [])),
            str(resource.get("mutation_count") or 0),
        )

    console.print(table)
    console.print(
        f"[dim]{len(resources)} resource(s) affected, "
        f"{diff.get('total_mutations_scanned') or diff.get('total_mutations') or 0} mutation(s) scanned[/dim]"
    )


def _board_payload(summary: BoardSummary) -> dict[str, Any]:
    """Convert a BoardSummary model to a JSON-safe payload.

    Args:
        summary: BoardSummary model from the SDK.

    Returns:
        JSON-serializable payload.
    """
    return summary.model_dump(mode="json")


def _task_payload(task: Task) -> dict[str, Any]:
    """Convert a Task model to a JSON-safe payload.

    Args:
        task: Task model from the SDK.

    Returns:
        JSON-serializable payload.
    """
    return task.model_dump(mode="json")


def _comment_payload(comment: TaskComment) -> dict[str, Any]:
    """Convert a TaskComment model to a JSON-safe payload.

    Args:
        comment: TaskComment model from the SDK.

    Returns:
        JSON-serializable payload.
    """
    return comment.model_dump(mode="json")


def _activity_payload(entry: TaskActivityEntry) -> dict[str, Any]:
    """Convert a TaskActivityEntry model to a JSON-safe payload.

    Args:
        entry: TaskActivityEntry model from the SDK.

    Returns:
        JSON-serializable payload.
    """
    return entry.model_dump(mode="json")


def _mutation_page_payload(page: TaskMutationPage) -> dict[str, Any]:
    """Convert a TaskMutationPage model to a JSON-safe payload.

    Args:
        page: TaskMutationPage model from the SDK.

    Returns:
        JSON-serializable payload preserving ``items`` and ``next_cursor``.
    """
    return page.model_dump(mode="json")


def _graph_diff_payload(diff: GraphDiff) -> dict[str, Any]:
    """Convert a GraphDiff model to a JSON-safe payload.

    Args:
        diff: GraphDiff model from the SDK.

    Returns:
        JSON-serializable payload.
    """
    return diff.model_dump(mode="json")


@app.command("board")
def board_command(
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
) -> None:
    """Show task counts per status for the current organization.

    Example:
        pragma tasks board
    """
    summary = get_client().get_task_board()
    output_data(_board_payload(summary), output, table_renderer=_print_board)


@app.command("list")
def list_command(
    status: Annotated[
        TaskStatus | None,
        typer.Option("--status", "-s", help="Filter by task status"),
    ] = None,
    assigned_to_instance_id: Annotated[
        str | None,
        typer.Option(
            "--assigned-to-instance-id",
            help="Filter to tasks assigned to this agent instance",
        ),
    ] = None,
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
) -> None:
    """List tasks for the current organization.

    Example:
        pragma tasks list
        pragma tasks list --status running
        pragma tasks list --assigned-to-instance-id inst_123
    """
    tasks = get_client().list_tasks(
        status=status,
        assigned_to_instance_id=assigned_to_instance_id,
    )

    if not tasks:
        console.print("[dim]No tasks found.[/dim]")
        return

    output_data(
        [_task_payload(task) for task in tasks],
        output,
        table_renderer=_print_task_list,
    )


@app.command("show")
def show_command(
    task_id: Annotated[str, typer.Argument(help="Task ID")],
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
) -> None:
    """Show detail for a single task.

    Example:
        pragma tasks show task:abc123

    Raises:
        typer.Exit: If the task is not found or the request fails.
    """
    try:
        task = get_client().get_task(task_id)
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            console.print(f"[red]Error:[/red] Task '{task_id}' not found.")
            raise typer.Exit(1) from error
        console.print(f"[red]Error:[/red] {_format_api_error(error)}")
        raise typer.Exit(1) from error

    output_data([_task_payload(task)], output, table_renderer=_print_task_detail)


@app.command("comments")
def comments_command(
    task_id: Annotated[str, typer.Argument(help="Task ID")],
    limit: Annotated[int, typer.Option("--limit", help="Maximum comments to return (1-200)")] = 50,
    cursor: Annotated[
        str | None,
        typer.Option("--cursor", help="Composite pagination cursor from a previous page"),
    ] = None,
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
) -> None:
    """List comments on a task, oldest first.

    Example:
        pragma tasks comments task:abc123
        pragma tasks comments task:abc123 --limit 100

    Raises:
        typer.Exit: If the task is not found or the request fails.
    """
    try:
        comments = get_client().list_task_comments(task_id, limit=limit, cursor=cursor)
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            console.print(f"[red]Error:[/red] Task '{task_id}' not found.")
            raise typer.Exit(1) from error
        console.print(f"[red]Error:[/red] {_format_api_error(error)}")
        raise typer.Exit(1) from error

    if not comments:
        console.print("[dim]No comments found.[/dim]")
        return

    output_data(
        [_comment_payload(comment) for comment in comments],
        output,
        table_renderer=_print_comments,
    )


@app.command("activity")
def activity_command(
    task_id: Annotated[str, typer.Argument(help="Task ID")],
    limit: Annotated[int, typer.Option("--limit", help="Maximum entries to return (1-200)")] = 50,
    cursor: Annotated[
        str | None,
        typer.Option("--cursor", help="Composite pagination cursor from a previous page"),
    ] = None,
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
) -> None:
    """Show the activity timeline for a task, newest first.

    Example:
        pragma tasks activity task:abc123

    Raises:
        typer.Exit: If the task is not found or the request fails.
    """
    try:
        entries = get_client().list_task_activity(task_id, limit=limit, cursor=cursor)
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            console.print(f"[red]Error:[/red] Task '{task_id}' not found.")
            raise typer.Exit(1) from error
        console.print(f"[red]Error:[/red] {_format_api_error(error)}")
        raise typer.Exit(1) from error

    if not entries:
        console.print("[dim]No activity found.[/dim]")
        return

    output_data(
        [_activity_payload(entry) for entry in entries],
        output,
        table_renderer=_print_activity,
    )


@app.command("mutations")
def mutations_command(
    task_id: Annotated[str, typer.Argument(help="Task ID")],
    limit: Annotated[int, typer.Option("--limit", help="Maximum mutations per page (1-200)")] = 50,
    cursor: Annotated[
        str | None,
        typer.Option("--cursor", help="Composite pagination cursor from a previous page"),
    ] = None,
    reveal: Annotated[
        bool,
        typer.Option(
            "--reveal",
            help="Show actual values for sensitive fields (otherwise masked)",
        ),
    ] = False,
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
) -> None:
    """Show the paginated mutation log for a task.

    Each entry carries a full before/after snapshot in JSON output.
    Sensitive fields are masked by default — pass ``--reveal`` to see
    actual values.

    Example:
        pragma tasks mutations task:abc123
        pragma tasks mutations task:abc123 --reveal -o json

    Raises:
        typer.Exit: If the task is not found or the request fails.
    """
    try:
        page = get_client().list_task_mutations(
            task_id,
            limit=limit,
            cursor=cursor,
            reveal=reveal,
        )
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            console.print(f"[red]Error:[/red] Task '{task_id}' not found.")
            raise typer.Exit(1) from error
        console.print(f"[red]Error:[/red] {_format_api_error(error)}")
        raise typer.Exit(1) from error

    output_data(_mutation_page_payload(page), output, table_renderer=_print_mutations)


@app.command("diff")
def diff_command(
    task_id: Annotated[str, typer.Argument(help="Task ID")],
    reveal: Annotated[
        bool,
        typer.Option(
            "--reveal",
            help="Show actual values for sensitive fields (otherwise masked)",
        ),
    ] = False,
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
) -> None:
    """Show the net delta per resource for a task.

    The server collapses every ``task->mutated->resource`` edge into a
    per-resource net delta — a create + many updates collapse to a
    single ``create``, a create + delete collapses to ``noop``, etc.

    The endpoint scans up to 5000 mutations per request. When the cap
    is hit the rollup is partial and a banner directs the caller to
    ``pragma tasks mutations`` for the full audit trail.

    Example:
        pragma tasks diff task:abc123
        pragma tasks diff task:abc123 --reveal -o json

    Raises:
        typer.Exit: If the task is not found or the request fails.
    """
    try:
        diff = get_client().get_task_graph_diff(task_id, reveal=reveal)
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            console.print(f"[red]Error:[/red] Task '{task_id}' not found.")
            raise typer.Exit(1) from error
        console.print(f"[red]Error:[/red] {_format_api_error(error)}")
        raise typer.Exit(1) from error

    output_data(_graph_diff_payload(diff), output, table_renderer=_print_graph_diff)


@app.command("subtasks")
def subtasks_command(
    task_id: Annotated[str, typer.Argument(help="Parent task ID")],
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
) -> None:
    """List direct subtasks of a task.

    Example:
        pragma tasks subtasks task:abc123

    Raises:
        typer.Exit: If the task is not found or the request fails.
    """
    try:
        subtasks = get_client().list_subtasks(task_id)
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            console.print(f"[red]Error:[/red] Task '{task_id}' not found.")
            raise typer.Exit(1) from error
        console.print(f"[red]Error:[/red] {_format_api_error(error)}")
        raise typer.Exit(1) from error

    if not subtasks:
        console.print("[dim]No subtasks found.[/dim]")
        return

    output_data(
        [_task_payload(task) for task in subtasks],
        output,
        table_renderer=_print_task_list,
    )


def _format_api_error(error: httpx.HTTPStatusError) -> str:
    """Render an HTTP error response into a single user-facing line.

    Falls back to the response text or generic ``str(error)`` when the
    body is not JSON-encoded.

    Args:
        error: HTTP status error raised by the SDK client.

    Returns:
        Single-line error message suitable for Rich-markup output.
    """
    try:
        body = error.response.json()
    except (json.JSONDecodeError, ValueError):
        return error.response.text or str(error)

    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, dict):
            message = detail.get("message")
            if isinstance(message, str):
                return message

    return str(error)


__all__ = ["app"]
