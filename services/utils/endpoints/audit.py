"""Ingestion audit endpoint.

Replicates Max AI's index_audit_info CTE pattern via SQLAlchemy ORM.
"""

import logging

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from services.shared.models import IngestionAudit

logger = logging.getLogger(__name__)


def get_audit_info(session: Session, project_id: int) -> list[dict]:
    """Get the latest ingestion status per source file for a project.

    Replicates Max AI's CTE (RankedBlobs) pattern using SQLAlchemy.
    """
    from sqlalchemy import and_

    # Subquery: latest audit per source_file
    latest_subq = (
        session.query(
            IngestionAudit.source_file,
            sa_func.max(IngestionAudit.created_at).label("max_date"),
        )
        .filter(IngestionAudit.project_id == project_id)
        .group_by(IngestionAudit.source_file)
        .subquery()
    )

    # Join to get full rows
    results = (
        session.query(IngestionAudit)
        .join(
            latest_subq,
            and_(
                IngestionAudit.source_file == latest_subq.c.source_file,
                IngestionAudit.created_at == latest_subq.c.max_date,
            ),
        )
        .filter(IngestionAudit.project_id == project_id)
        .all()
    )

    return [
        {
            "source_file": r.source_file,
            "status": r.status,
            "chunk_count": r.chunk_count,
            "error_message": r.error_message,
            "document_hash": r.document_hash,
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else None,
        }
        for r in results
    ]
