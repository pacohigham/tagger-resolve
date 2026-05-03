# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""Shared Resolve scripting helpers.

Factored out of main.py to eliminate duplication of the
save-reload-via-temp-project pattern.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


TAGGER_NATIVE_FIELDS = [
    "Keywords", "Description", "Scene", "Shot", "Angle", "Comments",
]


def save_and_reload_project(project_name: str) -> bool:
    """Save the current project, then force-reload it to rebuild keyword bins.

    Returns True if the reload succeeded. The caller must ensure a project
    is open before calling this.

    The Resolve scripting API has no way to close and reopen the active
    project in one step. LoadProject on the same name while it's already
    active returns the cached handle without reloading. The workaround is
    to create a disposable temp project (which switches the active project
    away), then LoadProject to pull the original fresh from disk, then
    delete the temp.
    """
    from resolve_connector import get_resolve

    resolve = get_resolve()
    if resolve is None:
        logger.warning("save_and_reload: get_resolve() returned None")
        return False

    pm = resolve.GetProjectManager()

    logger.info(f"Saving {project_name}...")
    saved = pm.SaveProject()
    if not saved:
        logger.error(
            f"SaveProject() returned False for {project_name!r}. "
            "Project was NOT closed. Save manually before reopening."
        )
        return False

    logger.info(f"Project saved: {project_name}")

    _TEMP = "__TFR_temp_reload__"

    if project_name == _TEMP:
        logger.warning("Active project is the temp project itself. Open your project manually.")
        return False

    # Clean up leftover temp from a prior failed reload
    if pm.LoadProject(_TEMP):
        pm.LoadProject(project_name)
        pm.DeleteProject(_TEMP)

    temp = pm.CreateProject(_TEMP)
    if not temp:
        logger.warning(
            "Could not create temp project for reload. "
            "Close and reopen the project in Resolve manually."
        )
        return False

    reloaded = pm.LoadProject(project_name)
    if not reloaded:
        logger.warning(
            f"LoadProject({project_name!r}) failed after switching to temp. "
            "Attempting to delete temp and recover."
        )

    pm.DeleteProject(_TEMP)

    if reloaded:
        logger.info(f"Project reloaded: {project_name}")
        return True

    logger.warning(
        f"LoadProject({project_name!r}) failed. Open the project manually."
    )
    return False
