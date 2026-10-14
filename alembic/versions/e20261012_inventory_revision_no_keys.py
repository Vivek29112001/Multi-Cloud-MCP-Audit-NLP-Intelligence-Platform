"""Append-only inventory history, live evidence, and removal of stored AWS keys.

Revision ID: e20261012a1b2
Revises: c8d9e0f1a2b3
"""
from alembic import op
import sqlalchemy as sa

revision = 'e20261012a1b2'
down_revision = 'c8d9e0f1a2b3'
branch_labels = None
depends_on = None


def upgrade():
    # Existing encrypted access keys are intentionally destroyed. Backups must be
    # handled separately by the operator; they cannot be sanitized by Alembic.
    op.execute("UPDATE aws_connections SET auth_type='TRANSIENT_KEYS', auth_status='REAUTH_REQUIRED' WHERE auth_type='PERSISTED_KEYS'")
    with op.batch_alter_table('aws_connections') as batch:
        batch.drop_column('credential_ciphertext')

    op.create_table(
        'aws_inventory_revisions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('workspace_id', sa.Integer(), sa.ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False),
        sa.Column('aws_connection_id', sa.Integer(), sa.ForeignKey('aws_connections.id', ondelete='CASCADE'), nullable=False),
        sa.Column('base_scan_id', sa.String(64), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('change_type', sa.String(40), nullable=False),
        sa.Column('changed_by_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('changed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('context_json', sa.JSON(), nullable=False),
        sa.Column('overrides_json', sa.JSON(), nullable=False),
        sa.Column('change_json', sa.JSON(), nullable=False),
        sa.UniqueConstraint('aws_connection_id', 'revision', name='uq_aws_inventory_revision'),
    )
    op.create_index('ix_aws_inventory_revisions_workspace_id', 'aws_inventory_revisions', ['workspace_id'])
    op.create_index('ix_aws_inventory_revisions_aws_connection_id', 'aws_inventory_revisions', ['aws_connection_id'])
    op.create_index('ix_aws_inventory_revisions_base_scan_id', 'aws_inventory_revisions', ['base_scan_id'])
    op.create_table(
        'aws_live_evidence',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('workspace_id', sa.Integer(), sa.ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False),
        sa.Column('aws_connection_id', sa.Integer(), sa.ForeignKey('aws_connections.id', ondelete='CASCADE'), nullable=False),
        sa.Column('inventory_revision_id', sa.Integer(), sa.ForeignKey('aws_inventory_revisions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('query_key', sa.String(64), nullable=False),
        sa.Column('intent_json', sa.JSON(), nullable=False),
        sa.Column('evidence_json', sa.JSON(), nullable=False),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_aws_live_evidence_workspace_id', 'aws_live_evidence', ['workspace_id'])
    op.create_index('ix_aws_live_evidence_aws_connection_id', 'aws_live_evidence', ['aws_connection_id'])
    op.create_index('ix_aws_live_evidence_inventory_revision_id', 'aws_live_evidence', ['inventory_revision_id'])
    op.create_index('ix_aws_live_evidence_query_key', 'aws_live_evidence', ['query_key'])


def downgrade():
    op.drop_table('aws_live_evidence')
    op.drop_table('aws_inventory_revisions')
    # Never restore previously stored credentials during downgrade.
    with op.batch_alter_table('aws_connections') as batch:
        batch.add_column(sa.Column('credential_ciphertext', sa.Text(), nullable=True))
