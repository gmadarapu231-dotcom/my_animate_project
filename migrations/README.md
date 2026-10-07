# Migrations

`init_db()` creates missing *tables*. It does not alter existing ones, so a
column added to a model after go-live will not appear in a database that
already has that table — and nothing will say so until a query selects it.

Every schema change after the first deployment goes through Alembic.

```bash
# see what the models want that the database does not have
alembic revision --autogenerate -m "add premium tax credit columns"

# read the generated file before applying it. Autogenerate is a draft:
# it does not see renames (it writes a drop and an add, which loses data),
# and it cannot know how to backfill.

alembic upgrade head        # apply
alembic downgrade -1        # step back one
alembic current             # what is applied now
```

## Rules for this repository

1. **Read every generated migration.** A rename rendered as drop-then-add
   destroys a column of client data, silently, and the test suite passes.
2. **Never edit a migration that has run in production.** Write another one.
3. **Back up before applying.** `pg_dump` takes seconds and a bad migration
   against a filing season's returns has no undo.
4. **Apply before the new code starts, not after.** The deploy order is
   migrate, then roll. A process that starts first will query a column that
   does not exist yet.
5. **Additive first.** Add a nullable column, backfill, make it non-null in a
   later migration. A single migration that adds a non-null column to a table
   with rows fails and leaves the deploy half-done.
