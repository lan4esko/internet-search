"""Create the control registry for isolated physical TASK databases."""
from alembic import op
import sqlalchemy as sa


revision = "0001_task_registry"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_registry",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("database_name", sa.String(length=63), nullable=False),
        sa.Column("source_filename", sa.String(length=255), nullable=False),
        sa.Column("source_path", sa.String(length=1024), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="provisioning"),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("target_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("open_port_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("result_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("database_name", name="uq_task_registry_database_name"),
    )
    op.create_index("ix_task_registry_status", "task_registry", ["status"])


def downgrade() -> None:
    op.drop_index("ix_task_registry_status", table_name="task_registry")
    op.drop_table("task_registry")
