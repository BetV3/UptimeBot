import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.models import AlertChannel, AlertType, Project, User
from app.schemas.alerts import AlertChannelCreate, AlertChannelUpdate, AlertChannelResponse
from app.services.alerts import send_test_alert
from app.services.auth import get_current_user

router = APIRouter()


def _channel_to_response(ch: AlertChannel) -> AlertChannelResponse:
    return AlertChannelResponse(
        id=str(ch.id),
        project_id=str(ch.project_id),
        type=ch.type.value,
        config=ch.config,
        is_active=ch.is_active,
        created_at=ch.created_at.isoformat(),
    )


async def _get_user_project(project_id: uuid.UUID, user: User, db: AsyncSession) -> Project:
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


async def _get_user_alert_channel(channel_id: uuid.UUID, user: User, db: AsyncSession) -> AlertChannel:
    result = await db.execute(
        select(AlertChannel)
        .join(Project, AlertChannel.project_id == Project.id)
        .where(AlertChannel.id == channel_id, Project.user_id == user.id)
    )
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert channel not found")
    return channel


@router.post(
    "/projects/{project_id}/alerts",
    response_model=AlertChannelResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_alert_channel(
    project_id: uuid.UUID,
    body: AlertChannelCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_user_project(project_id, current_user, db)
    channel = AlertChannel(
        project_id=project_id,
        type=AlertType(body.type),
        config=body.config,
        is_active=body.is_active,
    )
    db.add(channel)
    await db.commit()
    await db.refresh(channel)
    return _channel_to_response(channel)


@router.get("/projects/{project_id}/alerts", response_model=list[AlertChannelResponse])
async def list_alert_channels(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_user_project(project_id, current_user, db)
    result = await db.execute(
        select(AlertChannel)
        .where(AlertChannel.project_id == project_id)
        .order_by(AlertChannel.created_at.desc())
    )
    return [_channel_to_response(ch) for ch in result.scalars().all()]


@router.patch("/alerts/{channel_id}", response_model=AlertChannelResponse)
async def update_alert_channel(
    channel_id: uuid.UUID,
    body: AlertChannelUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    channel = await _get_user_alert_channel(channel_id, current_user, db)
    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(channel, field, value)
    await db.commit()
    await db.refresh(channel)
    return _channel_to_response(channel)


@router.delete("/alerts/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_alert_channel(
    channel_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    channel = await _get_user_alert_channel(channel_id, current_user, db)
    await db.delete(channel)
    await db.commit()


@router.post("/alerts/{channel_id}/test")
async def test_alert_channel(
    channel_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    channel = await _get_user_alert_channel(channel_id, current_user, db)

    # Get project name
    proj_result = await db.execute(
        select(Project.name).where(Project.id == channel.project_id)
    )
    project_name = proj_result.scalar() or "Unknown"

    try:
        await send_test_alert(channel, project_name)
        return {"status": "sent"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to send test alert: {str(e)}",
        )
