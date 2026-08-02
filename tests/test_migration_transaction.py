"""``alembic upgrade head`` must actually commit what it migrates.

The failure this guards is the quiet one: `_ensure_version_table_width` runs DDL
on the connection, which autobegins a transaction under SQLAlchemy 2.0
commit-as-you-go. `MigrationContext.configure` snapshots
`connection.in_transaction()` into `_in_external_transaction`, and when that is
true `begin_transaction()` returns a `nullcontext` — Alembic assumes the caller
owns the transaction and will commit it. Nobody did, so
`async with connectable.connect()` rolled the entire upgrade back on close.

`alembic upgrade head` exited 0 and logged every revision, and the database was
untouched. Deploys reported success against an unmigrated schema.

These run in the normal SQLite suite. SQLite's Alembic impl declares
``transactional_ddl = False``, which would make ``begin_transaction()`` a
nullcontext for reasons unrelated to the bug, so the migration context here is
configured with ``transactional_ddl=True`` to match the Postgres impl prod runs.
"""

import ast
from pathlib import Path

import pytest
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text

ENV_PY = Path(__file__).resolve().parent.parent / "alembic" / "env.py"


def _load_from_env_py(name: str, namespace: dict):
    """exec a single function out of ``alembic/env.py``.

    ``env.py`` runs migrations as a side effect of import — it is a script
    Alembic execs, not a module — so the function under test is lifted out of
    the parsed source instead of imported.
    """
    for node in ast.parse(ENV_PY.read_text()).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(ENV_PY), "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"alembic/env.py no longer defines {name}()")


class _RecordingConnection:
    """Just enough Connection to see whether the widening writes anything."""

    class _Dialect:
        def __init__(self, name):
            self.name = name

    def __init__(self, dialect_name):
        self.dialect = self._Dialect(dialect_name)
        self.statements: list[str] = []

    def execute(self, statement):
        self.statements.append(str(statement))


class _FakeEnvContext:
    """Stands in for Alembic's ``EnvironmentContext``, wrapping a real ``MigrationContext``.

    The transaction ownership logic under test lives in the real
    ``MigrationContext``; only the ``env.py``-facing surface is faked.
    """

    def __init__(self, migration_sql: str):
        self._sql = migration_sql
        self._mc = None
        self.in_transaction_at_configure = None

    def configure(self, connection=None, target_metadata=None, **kwargs):
        self.in_transaction_at_configure = connection.in_transaction()
        self._mc = MigrationContext.configure(
            connection=connection, opts={"transactional_ddl": True}
        )

    def begin_transaction(self):
        return self._mc.begin_transaction()

    def run_migrations(self, **kwargs):
        self._mc.connection.execute(text(self._sql))


def _widen_like_postgres(connection):
    """The postgres branch of ``_ensure_version_table_width``, in SQLite dialect.

    What matters is that it writes on the connection — the real one issues a
    ``CREATE TABLE IF NOT EXISTS`` plus an ``ALTER … TYPE VARCHAR(128)``, and
    the ``ALTER`` has no SQLite equivalent. The autobegin is identical.
    """
    connection.execute(text(
        "CREATE TABLE IF NOT EXISTS alembic_version ("
        " version_num VARCHAR(128) NOT NULL,"
        " CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
    ))


def test_widening_the_version_table_writes_on_the_connection():
    """The precondition for the whole bug: on Postgres, the widening autobegins.

    If this ever stops being true the commit below is merely harmless; if it
    stays true — it must, the column has to be widened before revision 037 —
    the commit is load-bearing.
    """
    ensure = _load_from_env_py(
        "_ensure_version_table_width",
        {"text": text, "_VERSION_NUM_WIDTH": 128},
    )

    postgres = _RecordingConnection("postgresql")
    ensure(postgres)
    assert postgres.statements, (
        "the widening no longer executes anything on Postgres — the "
        "transaction-ownership test below is now testing nothing"
    )

    sqlite = _RecordingConnection("sqlite")
    ensure(sqlite)
    assert sqlite.statements == [], "the widening must stay a Postgres-only no-op elsewhere"


@pytest.fixture()
def migration_engine(tmp_path):
    """A file-backed SQLite database — in-memory dies with the connection.

    Reopening it is the assertion: whatever the migration did not commit is
    gone, exactly as it was on prod.
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'migrations.db'}")
    with engine.connect() as connection:
        connection.execute(text("CREATE TABLE applied (revision VARCHAR(128) NOT NULL)"))
        connection.commit()
    yield engine
    engine.dispose()


def test_do_run_migrations_commits_what_it_migrated(migration_engine):
    """The regression itself: run the upgrade, close the connection, look again."""
    env_context = _FakeEnvContext("INSERT INTO applied (revision) VALUES ('099_example')")
    do_run_migrations = _load_from_env_py(
        "do_run_migrations",
        {
            "context": env_context,
            "target_metadata": None,
            "_ensure_version_table_width": _widen_like_postgres,
        },
    )

    with migration_engine.connect() as connection:
        do_run_migrations(connection)

    with migration_engine.connect() as connection:
        applied = connection.execute(text("SELECT revision FROM applied")).scalars().all()

    assert applied == ["099_example"], (
        "the migration was rolled back when the connection closed — "
        "'alembic upgrade head' exits 0 and commits nothing"
    )


def test_alembic_owns_the_migration_transaction(migration_engine):
    """Alembic must be handed a connection with no transaction in flight.

    Committing *after* ``context.configure`` (or at the end of the function)
    would leave the schema on disk while ``_in_external_transaction`` stays
    true, so ``begin_transaction()`` is still a nullcontext and every future
    caller inherits the trap. The commit belongs before configure.
    """
    env_context = _FakeEnvContext("INSERT INTO applied (revision) VALUES ('099_example')")
    do_run_migrations = _load_from_env_py(
        "do_run_migrations",
        {
            "context": env_context,
            "target_metadata": None,
            "_ensure_version_table_width": _widen_like_postgres,
        },
    )

    with migration_engine.connect() as connection:
        do_run_migrations(connection)

    assert env_context.in_transaction_at_configure is False, (
        "context.configure() saw an open transaction — Alembic will treat the "
        "caller as its owner and never commit the migrations itself"
    )
