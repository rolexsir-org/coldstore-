"""Re-add delivery_records.dispatch_id FK (dropped in 7e934dfc008c as part of
breaking the dispatch_notes.order_id <-> outward_orders.dispatch_id cycle;
this FK itself creates no cycle once outward_orders.dispatch_id is a plain
indexed integer)."""
from alembic import op
import sqlalchemy as sa

revision = 'c4d81f0a9b21'
down_revision = '7e934dfc008c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('delivery_records') as b:
        b.create_foreign_key(
            'fk_delivery_records_dispatch_id', 'dispatch_notes',
            ['dispatch_id'], ['id'], ondelete='RESTRICT')


def downgrade() -> None:
    with op.batch_alter_table('delivery_records') as b:
        b.drop_constraint('fk_delivery_records_dispatch_id', type_='foreignkey')
