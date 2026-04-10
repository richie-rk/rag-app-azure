"""SQLAlchemy ORM models for rag-app-azure.

Tables:
  users              — User accounts (SSO + magic link, no passwords)
  projects           — Project/index configuration
  user_project_access — Many-to-many user↔project with role
  ingestion_audit    — Document ingestion tracking
  magic_links        — Time-limited guest auth tokens
"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


# ── Users ─────────────────────────────────────────────────────────────────────


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    display_name = Column(String(255))
    auth_type = Column(String(20))  # "sso" | "magic_link"
    role = Column(String(20), default="user")  # "admin" | "user" | "guest"
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    last_login = Column(DateTime)

    project_access = relationship("UserProjectAccess", back_populates="user")


# ── Projects ──────────────────────────────────────────────────────────────────


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    display_name = Column(String(255))
    index_name = Column(String(255), unique=True, nullable=False)
    department = Column(String(255))
    system_prompt = Column(Text)
    example_questions = Column(Text)  # JSON array stored as string
    chunking_strategy = Column(String(50), default="page_wise")
    search_strategy = Column(String(50), default="hybrid")
    llm_deployment = Column(String(100), default="gpt-4o")
    is_default = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())

    user_access = relationship("UserProjectAccess", back_populates="project")
    audits = relationship("IngestionAudit", back_populates="project")


# ── User ↔ Project Access ────────────────────────────────────────────────────


class UserProjectAccess(Base):
    __tablename__ = "user_project_access"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    role = Column(String(20), default="viewer")  # "admin" | "editor" | "viewer"
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (UniqueConstraint("user_id", "project_id"),)

    user = relationship("User", back_populates="project_access")
    project = relationship("Project", back_populates="user_access")


# ── Ingestion Audit ──────────────────────────────────────────────────────────


class IngestionAudit(Base):
    __tablename__ = "ingestion_audit"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    source_file = Column(String(500), nullable=False)
    status = Column(String(50))  # "processing" | "completed" | "failed"
    chunk_count = Column(Integer, default=0)
    error_message = Column(Text)
    document_hash = Column(String(64))  # SHA-256 for dedup detection
    created_at = Column(DateTime, server_default=func.now())

    project = relationship("Project", back_populates="audits")


# ── Magic Links ──────────────────────────────────────────────────────────────


class MagicLink(Base):
    __tablename__ = "magic_links"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), nullable=False)
    token = Column(String(255), unique=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())
