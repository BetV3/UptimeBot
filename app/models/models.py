import uuid
import enum
from datetime import datetime

from sqlalchemy import (
    Column, String, Boolean, Integer, DateTime, ForeignKey, Enum, Text, func
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship

from app.core.database import Base


# --- Enums ---

class PlanType(str, enum.Enum):
    FREE = "free"
    STARTER = "starter"
    PRO = "pro"


class HttpMethod(str, enum.Enum):
    GET = "GET"
    POST = "POST"
    HEAD = "HEAD"


class MonitorStatus(str, enum.Enum):
    UP = "up"
    DOWN = "down"
    UNKNOWN = "unknown"


class CheckStatus(str, enum.Enum):
    UP = "up"
    DOWN = "down"


class CheckRegion(str, enum.Enum):
    US = "us"
    EU = "eu"
    ASIA = "asia"


class AlertType(str, enum.Enum):
    DISCORD_WEBHOOK = "discord_webhook"
    TELEGRAM = "telegram"
    EMAIL = "email"


# --- Models ---

class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    plan = Column(Enum(PlanType), default=PlanType.FREE, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    projects = relationship("Project", back_populates="user", cascade="all, delete-orphan")


class Project(Base):
    __tablename__ = "projects"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(100), nullable=False)
    slug = Column(String(100), unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="projects")
    monitors = relationship("Monitor", back_populates="project", cascade="all, delete-orphan")
    status_page = relationship("StatusPage", back_populates="project", uselist=False, cascade="all, delete-orphan")
    alert_channels = relationship("AlertChannel", back_populates="project", cascade="all, delete-orphan")


class Monitor(Base):
    __tablename__ = "monitors"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(100), nullable=False)
    url = Column(String(2048), nullable=False)
    method = Column(Enum(HttpMethod), default=HttpMethod.GET, nullable=False)
    expected_status = Column(Integer, default=200, nullable=False)
    interval_seconds = Column(Integer, default=60, nullable=False)
    timeout_seconds = Column(Integer, default=10, nullable=False)
    headers = Column(JSONB, nullable=True)
    body = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    current_status = Column(Enum(MonitorStatus), default=MonitorStatus.UNKNOWN, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    project = relationship("Project", back_populates="monitors")
    checks = relationship("Check", back_populates="monitor", cascade="all, delete-orphan")
    incidents = relationship("Incident", back_populates="monitor", cascade="all, delete-orphan")


class Check(Base):
    __tablename__ = "checks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    monitor_id = Column(UUID(as_uuid=True), ForeignKey("monitors.id", ondelete="CASCADE"), nullable=False)
    region = Column(Enum(CheckRegion), nullable=False)
    status = Column(Enum(CheckStatus), nullable=False)
    response_time_ms = Column(Integer, nullable=True)
    status_code = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    checked_at = Column(DateTime(timezone=True), server_default=func.now())

    monitor = relationship("Monitor", back_populates="checks")


class Incident(Base):
    __tablename__ = "incidents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    monitor_id = Column(UUID(as_uuid=True), ForeignKey("monitors.id", ondelete="CASCADE"), nullable=False)
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    monitor = relationship("Monitor", back_populates="incidents")


class StatusPage(Base):
    __tablename__ = "status_pages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, unique=True)
    is_public = Column(Boolean, default=False, nullable=False)
    display_name = Column(String(100), nullable=False)
    logo_url = Column(String(2048), nullable=True)
    primary_color = Column(String(7), default="#7C3AED", nullable=False)

    project = relationship("Project", back_populates="status_page")


class AlertChannel(Base):
    __tablename__ = "alert_channels"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    type = Column(Enum(AlertType), nullable=False)
    config = Column(JSONB, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    project = relationship("Project", back_populates="alert_channels")
