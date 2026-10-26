"""stars_orders: себестоимость в рублях по курсу TON на момент выдачи

Revision ID: 0138
Revises: 0137
Create Date: 2026-10-04

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0138'
down_revision: Union[str, None] = '0137'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Бывшая 0133: на базах, где она уже прошла, колонки есть.
    columns = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('stars_orders')}
    if 'ton_rate_kopeks' not in columns:
        op.add_column('stars_orders', sa.Column('ton_rate_kopeks', sa.Integer(), nullable=True))
    if 'cost_kopeks' not in columns:
        op.add_column('stars_orders', sa.Column('cost_kopeks', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column('stars_orders', 'cost_kopeks')
    op.drop_column('stars_orders', 'ton_rate_kopeks')
