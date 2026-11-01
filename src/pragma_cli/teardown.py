"""Teardown impact rendering and cascade progress for resource commands."""

from __future__ import annotations

import time
from dataclasses import dataclass

from pragma_sdk import LifecycleState, ProjectResources, ResourceFailedError, TeardownAction, TeardownImpact
from rich.console import Console


console = Console()

POLL_INTERVAL_SECONDS = 2.0
DEFAULT_WAIT_TIMEOUT_SECONDS = 300.0

ACTION_WORDING = {
    TeardownAction.TEARDOWN: "torn down",
    TeardownAction.WALK_THROUGH: "left in draft",
    TeardownAction.RELEASED: "kept, another owner still holds it",
    TeardownAction.DEPENDENT_WAITING: "moved to waiting until its dependency returns",
    TeardownAction.DEPENDENT_FAILED: "failed, its dependency is deleted",
}


@dataclass(frozen=True)
class TeardownOptions:
    """How a deactivate or delete command behaves.

    Attributes:
        wait: Block until the teardown finishes.
        wait_timeout: Seconds to wait before giving up; ``0`` waits forever.
        dry_run: Preview the teardown without changing anything.
    """

    wait: bool
    wait_timeout: float
    dry_run: bool


def print_impact(impact: list[TeardownImpact]) -> None:
    """Print one indented line per resource the teardown reaches.

    Args:
        impact: Impact rows returned by a deactivate or delete call.
    """
    if not impact:
        return

    console.print("Resources this reaches:")
    for row in impact:
        console.print(f"  {row.id} — {ACTION_WORDING[row.action]}")


def watch_teardown(
    project: ProjectResources,
    impact: list[TeardownImpact],
    *,
    timeout: float,
    settled_state: LifecycleState,
) -> None:
    """Block until every resource a teardown tears down has settled, reporting each one.

    Every teardown leaves the rows it reaches in place: a deactivation returns
    them to draft, a removal leaves them deleted until the archive claims them.
    ``settled_state`` names the state those rows settle in, and absence — an
    archive that won the race against the poll — still counts as settled.

    Args:
        project: Project-scoped SDK handle.
        impact: Impact rows returned by the deactivate or delete call.
        timeout: Seconds to wait before giving up; ``0`` waits forever.
        settled_state: State a row the teardown leaves in place settles in.

    Raises:
        ResourceFailedError: If a resource the teardown reaches fails.
        TimeoutError: If resources have not settled when ``timeout`` expires.
    """
    remaining = {row.id for row in impact if row.action == TeardownAction.TEARDOWN}
    deadline = time.monotonic() + timeout if timeout else None

    while remaining:
        present = {resource["id"]: resource for resource in project.list_resources()}
        settled = {
            resource_id
            for resource_id in remaining
            if resource_id not in present or present[resource_id].get("lifecycle_state") == settled_state.value
        }

        for resource_id in sorted(settled):
            console.print(f"  [green]done[/green] {resource_id}")
        remaining -= settled

        for resource_id in sorted(remaining):
            payload = present[resource_id]
            if payload.get("lifecycle_state") == LifecycleState.FAILED.value:
                raise ResourceFailedError(resource_id=resource_id, error=payload.get("error"), resource_data=payload)

        if not remaining:
            return

        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"{len(remaining)} resource(s) still tearing down after {timeout}s")

        time.sleep(POLL_INTERVAL_SECONDS)
