"""Tool and project access policy shared by call, run, and resource surfaces."""

from fastapi import HTTPException

from ...models import Tool
from ..identity.access import Caller


def _tool_allowed(caller: Caller, tool_name: str) -> bool:
    """Per-member tool ACL: allowed if the member's `tool_access` is unset (NULL = ALL tools) or names
    this tool. The OWNER is never restricted (the org's authority); admins/members can be."""
    if caller.role == "owner":
        return True
    access = caller.membership.tool_access
    return access is None or tool_name in access


def _require_tool_access(caller: Caller, tool_name: str) -> None:
    """Gate any use of a tool (proxy call + both run tiers) on the member's tool ACL."""
    if not _tool_allowed(caller, tool_name):
        raise HTTPException(status_code=403, detail=(
            f"you don't have access to the tool {tool_name!r} in this team — an admin can grant it "
            "(dashboard → Team, or `treg org access <you> --tools …`)"))


def _project_allowed(caller: Caller, tool: Tool) -> bool:
    """Per-member PROJECT scope, the coarse dial above the per-tool one.

    NULL `project_access` = the whole org (the default, so nothing changed when projects landed), and a
    tool with NULL `project_id` is ORG-WIDE and always in scope — which is every tool that existed
    before projects. Owner is never restricted, matching `_tool_allowed`. Pure: `project_access` holds
    project IDs, so this is a set test with no query, even on the proxy's hot path."""
    if caller.role == "owner":
        return True
    access = caller.membership.project_access
    return access is None or tool.project_id is None or tool.project_id in access


def _tool_usable(caller: Caller, tool: Tool) -> bool:
    """The two ACL axes compose as AND: the project scope AND the per-tool list must both allow it.
    `project_access=[X]` with `tool_access=NULL` therefore means "every tool in project X, including
    ones added later" — the composition that makes the coarse dial useful on its own."""
    return _tool_allowed(caller, tool.name) and _project_allowed(caller, tool)


def _require_tool_use(caller: Caller, tool: Tool) -> None:
    """Gate any use of a tool (proxy call + both run tiers) on BOTH ACL axes."""
    _require_tool_access(caller, tool.name)
    if not _project_allowed(caller, tool):
        raise HTTPException(status_code=403, detail=(
            f"the tool {tool.name!r} belongs to a project you're not scoped to — an admin can grant it "
            "(dashboard → Team, or `treg org access <you> --projects …`)"))
