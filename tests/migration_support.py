from importlib.resources import files

from alembic.script import ScriptDirectory


def schema_head() -> str:
    head, = ScriptDirectory(str(files("meta_research.migrations"))).get_heads()
    return head
