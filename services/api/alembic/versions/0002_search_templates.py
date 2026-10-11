"""Persist reusable scanner configuration templates."""
from alembic import op
import sqlalchemy as sa


revision = "0002_search_templates"
down_revision = "0001_task_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "search_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("name", name="uq_search_templates_name"),
    )


def downgrade() -> None:
    op.drop_table("search_templates")
