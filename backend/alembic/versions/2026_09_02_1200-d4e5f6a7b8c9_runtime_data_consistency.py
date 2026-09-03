"""runtime data consistency and retrieval indexes

Revision ID: d4e5f6a7b8c9
Revises: 293138585702
Create Date: 2026-09-02 12:00:00.000000+00:00

This migration is deliberately safe for both a clean install and databases that
were created from the older model metadata.  It repairs the model/migration
drift, removes historical duplicates before adding unique constraints, and adds
the indexes used by the current RAG queries.
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "293138585702"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Keep this here as well as in the initial migration so an already-versioned
    # database that missed extension provisioning can still be repaired.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute(
        "ALTER TABLE meetings "
        "ADD COLUMN IF NOT EXISTS transcription_mode VARCHAR(20)"
    )
    op.execute(
        "ALTER TABLE decisions "
        "ADD COLUMN IF NOT EXISTS evidence JSONB"
    )
    op.execute(
        "ALTER TABLE agent_runs ALTER COLUMN started_at DROP DEFAULT"
    )
    op.execute(
        "ALTER TABLE agent_runs ALTER COLUMN started_at DROP NOT NULL"
    )

    # Older runs could create more than one summary or repeat the same ASR
    # sequence.  Retain the newest row deterministically before enforcing the
    # application invariants at the database boundary.
    op.execute(
        """
        DELETE FROM summaries AS stale
        USING summaries AS keep
        WHERE stale.meeting_id = keep.meeting_id
          AND (stale.created_at, stale.id) < (keep.created_at, keep.id)
        """
    )
    op.execute(
        """
        DELETE FROM transcripts AS stale
        USING transcripts AS keep
        WHERE stale.meeting_id = keep.meeting_id
          AND stale.seq_index = keep.seq_index
          AND (stale.created_at, stale.id) < (keep.created_at, keep.id)
        """
    )

    # DO blocks make the repair idempotent for databases that were manually
    # patched before this Alembic revision was introduced.
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'uq_summaries_meeting_id'
              AND conrelid = 'summaries'::regclass
          ) THEN
            ALTER TABLE summaries
              ADD CONSTRAINT uq_summaries_meeting_id UNIQUE (meeting_id);
          END IF;
        END $$
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'uq_transcripts_meeting_seq'
              AND conrelid = 'transcripts'::regclass
          ) THEN
            ALTER TABLE transcripts
              ADD CONSTRAINT uq_transcripts_meeting_seq
              UNIQUE (meeting_id, seq_index);
          END IF;
        END $$
        """
    )

    # Align the ORM cascade contract with the database.  The generated name is
    # stable on PostgreSQL for the original migration.
    op.execute(
        "ALTER TABLE decisions "
        "DROP CONSTRAINT IF EXISTS decisions_meeting_id_fkey"
    )
    op.execute(
        "ALTER TABLE decisions ADD CONSTRAINT decisions_meeting_id_fkey "
        "FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE"
    )

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_action_items_meeting_id "
        "ON action_items (meeting_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_risks_meeting_id ON risks (meeting_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_decision_relations_target "
        "ON decision_relations (target_decision_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_documents_source "
        "ON knowledge_documents (source_type, source_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_documents_content_fts "
        "ON knowledge_documents USING GIN "
        "(to_tsvector('simple', content))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_documents_embedding_hnsw "
        "ON knowledge_documents USING hnsw (embedding vector_cosine_ops) "
        "WHERE embedding IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_knowledge_documents_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS ix_knowledge_documents_content_fts")
    op.execute("DROP INDEX IF EXISTS ix_knowledge_documents_source")
    op.execute("DROP INDEX IF EXISTS ix_decision_relations_target")
    op.execute("DROP INDEX IF EXISTS ix_risks_meeting_id")
    op.execute("DROP INDEX IF EXISTS ix_action_items_meeting_id")

    op.execute(
        "UPDATE agent_runs SET started_at = created_at WHERE started_at IS NULL"
    )
    op.execute(
        "ALTER TABLE agent_runs ALTER COLUMN started_at SET DEFAULT now()"
    )
    op.execute(
        "ALTER TABLE agent_runs ALTER COLUMN started_at SET NOT NULL"
    )

    op.execute(
        "ALTER TABLE decisions DROP CONSTRAINT IF EXISTS decisions_meeting_id_fkey"
    )
    op.execute(
        "ALTER TABLE decisions ADD CONSTRAINT decisions_meeting_id_fkey "
        "FOREIGN KEY (meeting_id) REFERENCES meetings(id)"
    )
    op.execute(
        "ALTER TABLE transcripts "
        "DROP CONSTRAINT IF EXISTS uq_transcripts_meeting_seq"
    )
    op.execute(
        "ALTER TABLE summaries "
        "DROP CONSTRAINT IF EXISTS uq_summaries_meeting_id"
    )
    op.execute("ALTER TABLE decisions DROP COLUMN IF EXISTS evidence")
    op.execute("ALTER TABLE meetings DROP COLUMN IF EXISTS transcription_mode")
    # Do not drop the shared vector extension: other schemas may depend on it.
