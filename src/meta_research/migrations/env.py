from __future__ import annotations

from alembic import context


def run_migrations_online() -> None:
    connection = context.config.attributes["connection"]
    context.configure(
        connection=connection,
        target_metadata=None,
        compare_type=True,
        render_as_batch=False,
        transactional_ddl=True,
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations_online()
