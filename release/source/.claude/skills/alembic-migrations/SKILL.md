---
name: alembic-migrations
category: Backend
description: "MUST USE when creating, editing, or reviewing Alembic migration scripts, env.py configuration, or SQLAlchemy model changes that require migrations. Enforces naming conventions, autogenerate review, data migration safety, downgrade correctness, and production deployment patterns."
---

# Alembic Migration Best Practices

## Naming Conventions — Define Once

```python
# BAD: no naming convention — anonymous constraints break downgrades
class Base(DeclarativeBase):
    pass

# GOOD: explicit naming convention
convention = {
    "ix": "ix_%(table_name)s_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=convention)
```

## File Naming — Use Timestamps

```ini
# alembic.ini
file_template = %%(epoch)d_%%(rev)s_%%(slug)s
```

```bash
# BAD
alembic revision --autogenerate -m "changes"
# GOOD
alembic revision --autogenerate -m "add_phone_column_to_users"
```

## Autogenerate — Always Review

**Detects:** table/column add/remove, nullable changes, indexes, foreign keys.

**Cannot detect:** renames (renders as drop + create = **data loss**), enum changes, standalone CHECK/EXCLUDE constraints.

**Enable in env.py:**
```python
context.configure(
    connection=connection,
    target_metadata=target_metadata,
    compare_type=True,
    compare_server_default=True,
    render_as_batch=True,  # required for SQLite
)
```

### Fix Renames Manually

```python
# BAD: autogenerate output — DATA LOSS
def upgrade():
    op.drop_column("users", "name")
    op.add_column("users", sa.Column("full_name", sa.String(100)))

# GOOD: rename
def upgrade():
    op.alter_column("users", "name", new_column_name="full_name")
def downgrade():
    op.alter_column("users", "full_name", new_column_name="name")
```

## Complete Downgrades — Always

Reverse operations in **opposite order** of upgrade.

```python
# BAD
def downgrade():
    pass

# GOOD
def upgrade():
    op.add_column("users", sa.Column("phone", sa.String(20), nullable=True))
    op.create_index("ix_users_phone", "users", ["phone"])

def downgrade():
    op.drop_index("ix_users_phone", table_name="users")
    op.drop_column("users", "phone")
```

## Data Migrations — Never Import App Models

Models change over time. Use inline table definitions or raw SQL.

```python
# BAD: breaks when model changes later
from myapp.models import User
def upgrade():
    users = User.query.all()

# GOOD: inline table definition
def upgrade():
    user_table = table("users", column("id", sa.Integer), column("email", String))
    conn = op.get_bind()
    conn.execute(user_table.update().values(email=func.lower(user_table.c.email)))

# GOOD: raw SQL
def upgrade():
    op.execute("UPDATE users SET email = LOWER(email) WHERE email IS NOT NULL")
```

### Batch Large Data Migrations

```python
# BAD: single UPDATE on millions of rows — locks table
op.execute("UPDATE orders SET status = 'active' WHERE status IS NULL")

# GOOD: batch processing
def upgrade():
    conn = op.get_bind()
    while True:
        result = conn.execute(sa.text(
            "UPDATE orders SET status = 'active' "
            "WHERE id IN (SELECT id FROM orders WHERE status IS NULL LIMIT 1000)"
        ))
        if result.rowcount == 0:
            break
```

## Separate Schema and Data Migrations

Never mix DDL and DML in the same migration.

```bash
alembic revision --autogenerate -m "add_status_column_to_orders"
alembic revision -m "backfill_status_column_on_orders"
```

## Add Non-Nullable Column — Three Steps

```python
def upgrade():
    op.add_column("users", sa.Column("role", sa.String(20), nullable=True))
    op.execute("UPDATE users SET role = 'member' WHERE role IS NULL")
    op.alter_column("users", "role", nullable=False)

def downgrade():
    op.drop_column("users", "role")
```

## Concurrent Indexes (PostgreSQL)

```python
def upgrade():
    op.execute("CREATE INDEX CONCURRENTLY ix_orders_user_id ON orders (user_id)")
```

Note: Cannot run inside a transaction — may need autocommit mode.

## Testing

### Stairway Test

```python
def test_stairway(alembic_config):
    script = ScriptDirectory.from_config(alembic_config)
    revisions = list(script.walk_revisions("base", "heads"))
    revisions.reverse()
    for revision in revisions:
        command.upgrade(alembic_config, revision.revision)
        command.downgrade(alembic_config, revision.down_revision or "base")
        command.upgrade(alembic_config, revision.revision)
```

```bash
# CI: detect pending migrations
alembic check
```

## Production

```yaml
# Docker: migrations before app start
app_migrations:
  command: ["alembic", "upgrade", "head"]
  depends_on:
    db: { condition: service_healthy }
app:
  depends_on:
    app_migrations: { condition: service_completed_successfully }
```

```bash
# Adopt Alembic on existing DB
alembic stamp head

# Merge diverged branches
alembic merge -m "merge_heads" head1 head2
```

## Rules

1. **Define naming conventions** on `MetaData` — predictable constraint names
2. **Use timestamp file templates** in `alembic.ini`
3. **Review every autogenerated migration** — autogenerate misses renames
4. **Enable `compare_type` and `compare_server_default`** in env.py
5. **Fix renames manually** — autogenerate renders them as drop + create (data loss)
6. **Write complete downgrades** — reverse every operation in opposite order
7. **Never import app models** in migrations — use `table()/column()` or raw SQL
8. **Separate schema and data migrations** — never mix DDL and DML
9. **Three-step non-nullable columns** — add nullable, backfill, set NOT NULL
10. **Batch large data migrations** — avoid long locks
11. **Run stairway tests and `alembic check` in CI**
