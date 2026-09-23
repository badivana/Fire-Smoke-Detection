"""Alembic environment. DB URL: `-x db_url=...` > alembic.ini > DATABASE_URL setting."""

from logging.config import fileConfig

from alembic import context

from app.core.config import get_settings
from app.db import Base
from app.db.base import UTCDateTime
from app.db.session import make_engine

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _db_url() -> str:
    return (
        context.get_x_argument(as_dictionary=True).get("db_url")
        or config.get_main_option("sqlalchemy.url")
        or get_settings().database_url
    )


def _render_item(type_, obj, autogen_context):
    """Render app-specific column types as plain SQLAlchemy types.

    Migrations must not import app code: they have to keep working after the app changes.
    """
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def _configure(**kw) -> None:
    context.configure(
        target_metadata=target_metadata,
        render_item=_render_item,
        render_as_batch=True,  # SQLite cannot ALTER most things; batch mode rebuilds tables
        compare_type=True,
        **kw,
    )


def run_migrations_offline() -> None:
    _configure(url=_db_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:  # provided by tests
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = make_engine(_db_url())
    with engine.connect() as conn:
        _configure(connection=conn)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
