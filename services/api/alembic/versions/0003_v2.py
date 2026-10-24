"""Add v2 without modifying v1 TASKs or physical databases."""
from alembic import op
revision='0003_v2'
down_revision='0002_search_templates'
branch_labels=None
depends_on=None

def upgrade():
    op.execute('''CREATE TABLE v2_tasks(id BIGSERIAL PRIMARY KEY,name TEXT NOT NULL,database_name TEXT NOT NULL UNIQUE,
     status TEXT NOT NULL DEFAULT 'provisioning',config JSONB NOT NULL,manifest JSONB NOT NULL,templates JSONB NOT NULL,
     source_filename TEXT NOT NULL,source_sha256 TEXT NOT NULL,error TEXT,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
     started_at TIMESTAMPTZ,finished_at TIMESTAMPTZ)''')
    op.execute('''CREATE TABLE v2_templates(id TEXT PRIMARY KEY,definition JSONB NOT NULL,sha256 TEXT NOT NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT now())''')
def downgrade():
    op.execute('DROP TABLE v2_templates');op.execute('DROP TABLE v2_tasks')
