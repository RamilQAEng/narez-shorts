"""Project endpoints (self-host): list, create, update, delete. See projects.py."""
from fastapi import APIRouter, Body, HTTPException

import projects

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("")
def list_projects():
    return {"projects": projects.list_all()}


@router.post("")
def create_project(body: dict = Body(...)):
    try:
        return projects.create(body)
    except projects.ProjectError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/{project_id}")
def update_project(project_id: str, body: dict = Body(...)):
    try:
        project = projects.update(project_id, body)
    except projects.ProjectError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.delete("/{project_id}")
def delete_project(project_id: str):
    if not projects.delete(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return {"deleted": project_id}
