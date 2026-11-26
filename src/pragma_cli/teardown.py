"""Teardown impact rendering and cascade progress for resource commands."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from pragma_sdk import LifecycleState, ProjectResources, ResourceFailedError, TeardownAction, TeardownImpact
from rich.console import Console


console = Console()

POLL_INTERVAL_SECONDS = 2.0
DEFAULT_WAIT_TIMEOUT_SECONDS = 300.0

ACTION_WORDING = {
    TeardownAction.TEARDOWN: "torn down",
    TeardownAction.WALK_THROUGH: "left in draft",
    TeardownAction.DEPENDENT_WAITING: "moved to waiting until its dependency returns",
    TeardownAction.DEPENDENT_FAILED: "failed, its dependency is deleted",
}

DEACTIVATION_SETTLED_LABELS: Mapping[LifecycleState, str] = MappingProxyType({
    LifecycleState.DRAFT: "done",
    LifecycleState.DELETED: "deleted",
})
DELETION_SETTLED_LABELS: Mapping[LifecycleState, str] = MappingProxyType({LifecycleState.DELETED: "done"})


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

    Each line names the resource, the state it is in before the teardown and
    what the teardown does to it: ``  <id> (<state>) — <action>``.

    Args:
        impact: Impact rows returned by a deactivate or delete call.
    """
    if not impact:
        return

    console.print("Resources this reaches:")
    for row in impact:
        console.print(f"  {row.id} ({row.lifecycle_state.value}) — {ACTION_WORDING[row.action]}")


def compute_observed_state(present: Mapping[str, dict], resource_id: str) -> LifecycleState:
    """Name the state a polled resource is in, counting one the poll did not list as deleted.

    Args:
        present: Resources the last poll listed, by identity.
        resource_id: Identity of the resource to look up.

    Returns:
        The resource's lifecycle state, or ``DELETED`` when the poll did not
        list it.
    """
    if resource_id not in present:
        return LifecycleState.DELETED
    return LifecycleState(present[resource_id]["lifecycle_state"])


def watch_teardown(
    project: ProjectResources,
    impact: list[TeardownImpact],
    *,
    requested_id: str,
    timeout: float,
    settled_labels: Mapping[LifecycleState, str],
) -> LifecycleState | None:
    """Block until every resource a teardown tears down has settled, reporting each one.

    Every teardown leaves the rows it reaches in place: a deactivation returns
    them to draft, a deletion leaves them deleted until the archive claims
    them. A delete accepted while a resource is deactivating takes it over, so
    a deactivation also settles a resource that ends deleted
    (``DEACTIVATION_SETTLED_LABELS``). A resource that no longer resolves
    counts as deleted.

    Args:
        project: Project-scoped SDK handle.
        impact: Impact rows returned by the deactivate or delete call.
        requested_id: Identity of the resource the command named, in
            ``org/provider/resource/name`` form.
        timeout: Seconds to wait before giving up; ``0`` waits forever.
        settled_labels: Each state a torn-down resource settles in, and the
            label its settled line is printed with.

    Returns:
        The state the requested resource settled in, or None when the
        teardown did not tear it down.

    Raises:
        ResourceFailedError: If a resource the teardown reaches fails.
        TimeoutError: If resources have not settled when ``timeout`` expires.
    """
    remaining = {row.id for row in impact if row.action == TeardownAction.TEARDOWN}
    requested_state: LifecycleState | None = None
    deadline = time.monotonic() + timeout if timeout else None

    while remaining:
        present = {resource["id"]: resource for resource in project.list_resources()}
        observed = {resource_id: compute_observed_state(present, resource_id) for resource_id in remaining}
        settled = {resource_id: state for resource_id, state in observed.items() if state in settled_labels}

        for resource_id in sorted(settled):
            console.print(f"  [green]{settled_labels[settled[resource_id]]}[/green] {resource_id}")
        requested_state = settled.get(requested_id, requested_state)
        remaining -= settled.keys()

        for resource_id in sorted(remaining):
            if observed[resource_id] == LifecycleState.FAILED:
                payload = present[resource_id]
                raise ResourceFailedError(resource_id=resource_id, error=payload.get("error"), resource_data=payload)

        if not remaining:
            break

        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"{len(remaining)} resource(s) still tearing down after {timeout}s")

        time.sleep(POLL_INTERVAL_SECONDS)

    return requested_state
