import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.models import User, Project, StatusPage
from app.schemas.projects import ProjectCreate, ProjectUpdate, ProjectResponse
from app.services.auth import get_current_user

router = APIRouter()


def slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


async def _get_project_or_404(
    project_id: uuid.UUID,
    user: User,
    db: AsyncSession,
) -> Project:
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    slug = slugify(body.name)
    # Ensure slug uniqueness by appending short suffix if needed
    result = await db.execute(select(Project).where(Project.slug == slug))
    if result.scalar_one_or_none():
        slug = f"{slug}-{uuid.uuid4().hex[:6]}"

    project = Project(user_id=current_user.id, name=body.name, slug=slug)
    db.add(project)
    await db.flush()

    # Auto-create status page with sensible defaults
    sp = StatusPage(project_id=project.id, display_name=body.name, is_public=False)
    db.add(sp)

    await db.commit()
    await db.refresh(project)
    return ProjectResponse(
        id=str(project.id),
        name=project.name,
        slug=project.slug,
        created_at=project.created_at.isoformat(),
    )


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Project).where(Project.user_id == current_user.id).order_by(Project.created_at.desc())
    )
    projects = result.scalars().all()
    return [
        ProjectResponse(
            id=str(p.id), name=p.name, slug=p.slug, created_at=p.created_at.isoformat()
        )
        for p in projects
    ]


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = await _get_project_or_404(project_id, current_user, db)
    return ProjectResponse(
        id=str(project.id),
        name=project.name,
        slug=project.slug,
        created_at=project.created_at.isoformat(),
    )


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: uuid.UUID,
    body: ProjectUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = await _get_project_or_404(project_id, current_user, db)
    if body.name is not None:
        project.name = body.name
        project.slug = slugify(body.name)
        # Check slug uniqueness
        result = await db.execute(
            select(Project).where(Project.slug == project.slug, Project.id != project.id)
        )
        if result.scalar_one_or_none():
            project.slug = f"{project.slug}-{uuid.uuid4().hex[:6]}"
    await db.commit()
    await db.refresh(project)
    return ProjectResponse(
        id=str(project.id),
        name=project.name,
        slug=project.slug,
        created_at=project.created_at.isoformat(),
    )


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = await _get_project_or_404(project_id, current_user, db)
    await db.delete(project)
    await db.commit()
