"""uk_documents_live_checksum: narrow documents checksum uniqueness to a partial unique index.

2026-09-28 kb B6 batch (tombstone re-upload, ocr review item 10). documents.valid_to seal
is the tombstone (soft delete, no physical delete). The old full-table UNIQUE constraint
keeps tombstoned rows occupying uniqueness, so re-uploading identical content after a
delete hits IntegrityError and the dedupe lookup returns the sealed row. Narrow uniqueness
to live rows (valid_to IS NULL): a tombstoned document can be re-registered as a brand-new
row while the tombstone row stays for audit.

Contract: docs/database/01 documents section (2026-09-28, B6 batch, partial unique index).
ORM parity: services/kb/data/orm.py Document.__table_args__ (same explicit index name).

Revision ID: c4f6a8b0d2e4
Revises: b2d4e6f8a0c1
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4f6a8b0d2e4"
down_revision: str | None = "b2d4e6f8a0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Explicit naming: the partial unique index reuses the dropped constraint's name so that
# ORM metadata, tests and the physical schema stay byte-identical on this key.
_UK_NAME = "uk_documents_tenant_id_kb_collection_id_checksum_sha256"
_COLUMNS = ["tenant_id", "kb_collection_id", "checksum_sha256"]


def upgrade() -> None:
    # Full-table UNIQUE constraint -> partial unique index of the same name:
    # tombstoned rows (valid_to sealed) no longer occupy uniqueness.
    op.drop_constraint(_UK_NAME, "documents", type_="unique")
    op.create_index(
        _UK_NAME,
        "documents",
        _COLUMNS,
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
    )


def downgrade() -> None:
    # Restore full-table uniqueness: requires no tombstone duplicates on this key.
    # Tombstone rows created before the downgrade are retained by design; de-duplicate
    # manually (re-seal or clear checksums) if such duplicates exist.
    op.drop_index(_UK_NAME, table_name="documents")
    op.create_unique_constraint(_UK_NAME, "documents", _COLUMNS)
