import uuid
import enum

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


class MonitorType(str, enum.Enum):
    HTTP = "http"
    SSL = "ssl"
    # DNS, API added in later phases as dedicated columns land.


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
    SLACK = "slack"
    WEBHOOK = "webhook"


# --- Models ---

class SubscriptionStatus(str, enum.Enum):
    ACTIVE = "active"
    TRIALING = "trialing"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"
    INCOMPLETE_EXPIRED = "incomplete_expired"
    UNPAID = "unpaid"


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    plan = Column(Enum(PlanType), default=PlanType.FREE, nullable=False)
    email_verified = Column(Boolean, default=False, nullable=False)
    verification_token = Column(String(128), nullable=True, index=True)
    verification_token_expires_at = Column(DateTime(timezone=True), nullable=True)
    password_reset_token = Column(String(128), nullable=True, index=True)
    password_reset_expires_at = Column(DateTime(timezone=True), nullable=True)
    stripe_customer_id = Column(String(255), nullable=True, index=True)
    stripe_subscription_id = Column(String(255), nullable=True, index=True)
    subscription_status = Column(
        Enum(SubscriptionStatus, values_callable=lambda e: [x.value for x in e]),
        nullable=True,
    )
    current_period_end = Column(DateTime(timezone=True), nullable=True)
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
    type = Column(Enum(MonitorType), default=MonitorType.HTTP, nullable=False)
    url = Column(String(2048), nullable=False)
    method = Column(Enum(HttpMethod), default=HttpMethod.GET, nullable=False)
    expected_status = Column(Integer, default=200, nullable=False)
    interval_seconds = Column(Integer, default=60, nullable=False)
    timeout_seconds = Column(Integer, default=10, nullable=False)
    headers = Column(JSONB, nullable=True)
    body = Column(Text, nullable=True)
    target_host = Column(String(255), nullable=True)
    target_port = Column(Integer, nullable=True)
    warn_days_before_expiry = Column(Integer, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    current_status = Column(Enum(MonitorStatus), default=MonitorStatus.UNKNOWN, nullable=False)
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    next_check_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    project = relationship("Project", back_populates="monitors")
    checks = relationship("Check", back_populates="monitor", cascade="all, delete-orphan")
    incidents = relationship("Incident", back_populates="monitor", cascade="all, delete-orphan")


class Check(Base):
    __tablename__ = "checks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    monitor_id = Column(UUID(as_uuid=True), ForeignKey("monitors.id", ondelete="CASCADE"), nullable=False, index=True)
    region = Column(Enum(CheckRegion), nullable=False)
    status = Column(Enum(CheckStatus), nullable=False)
    response_time_ms = Column(Integer, nullable=True)
    status_code = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    cert_days_remaining = Column(Integer, nullable=True)
    cert_subject = Column(String(512), nullable=True)
    cert_issuer = Column(String(512), nullable=True)
    checked_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

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


class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(100), nullable=False)
    key_hash = Column(String(255), nullable=False)
    prefix = Column(String(8), nullable=False, index=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User")


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    region = Column(Enum(CheckRegion), nullable=False, unique=True)
    hostname = Column(String(255), nullable=True)
    last_seen = Column(DateTime(timezone=True), server_default=func.now())
    version = Column(String(50), nullable=True)


class PendingCheck(Base):
    __tablename__ = "pending_checks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    monitor_id = Column(UUID(as_uuid=True), ForeignKey("monitors.id", ondelete="CASCADE"), nullable=False, index=True)
    region = Column(Enum(CheckRegion), nullable=False)
    scheduled_at = Column(DateTime(timezone=True), server_default=func.now())
    leased_at = Column(DateTime(timezone=True), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    worker_id = Column(String(255), nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    dead = Column(Boolean, nullable=False, default=False, server_default="false")

    monitor = relationship("Monitor")


class AlertDeliveryKind(str, enum.Enum):
    DOWN = "down"
    RESOLVED = "resolved"


class AlertDeliveryState(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class AlertDelivery(Base):
    __tablename__ = "alert_deliveries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id = Column(UUID(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False)
    alert_channel_id = Column(UUID(as_uuid=True), ForeignKey("alert_channels.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(16), nullable=False)
    state = Column(String(16), nullable=False, default=AlertDeliveryState.PENDING.value, server_default="pending")
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    sent_at = Column(DateTime(timezone=True), nullable=True)

    incident = relationship("Incident")
    alert_channel = relationship("AlertChannel")
