"""create stars_orders table

Revision ID: 0132
Revises: 0131
Create Date: 2026-10-04

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0132'
down_revision: Union[str, None] = '0131'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'stars_orders',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('recipient_username', sa.String(64), nullable=False),
        sa.Column('recipient_name', sa.String(255), nullable=True),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='paid'),
        sa.Column('source', sa.String(20), nullable=False, server_default='bot'),
        sa.Column('idempotency_key', sa.String(128), nullable=False),
        sa.Column(
            'transaction_id', sa.Integer(), sa.ForeignKey('transactions.id', ondelete='SET NULL'), nullable=True
        ),
        sa.Column(
            'refund_transaction_id',
            sa.Integer(),
            sa.ForeignKey('transactions.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('fragment_req_id', sa.String(128), nullable=True),
        sa.Column('ton_tx_hash', sa.String(128), nullable=True),
        sa.Column('cost_nanoton', sa.BigInteger(), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('processing_started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('refunded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_stars_orders_status_next_attempt', 'stars_orders', ['status', 'next_attempt_at'])
    op.create_index('ix_stars_orders_user_created', 'stars_orders', ['user_id', 'created_at'])
    op.create_index('ux_stars_orders_idempotency_key', 'stars_orders', ['idempotency_key'], unique=True)


def downgrade() -> None:
    op.drop_index('ux_stars_orders_idempotency_key', table_name='stars_orders')
    op.drop_index('ix_stars_orders_user_created', table_name='stars_orders')
    op.drop_index('ix_stars_orders_status_next_attempt', table_name='stars_orders')
    op.drop_table('stars_orders')
