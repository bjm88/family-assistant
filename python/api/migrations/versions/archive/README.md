# Archived migrations

These files are the original 0002 → 0028 alembic revisions, preserved
verbatim for historical reference. **They are not part of the active
migration chain.** Alembic does not scan subdirectories of `versions/`,
so dropping them here removes them from the chain without losing the
git history of how the schema evolved.

Why they were archived
----------------------

The original `0001_initial_schema.py` built the schema by calling
`Base.metadata.create_all()` against the *current* ORM rather than
hand-writing `op.create_table` for the historical shape. As the ORM
evolved the chain became un-replayable from an empty DB:

* `0002_family_tree_and_photos.py` renames a column
  (`relationship_to_head_of_household` → `primary_family_relationship`)
  that the modern ORM no longer defines, so `0001` never produces it
  and `0002` crashes on `alter_column`.
* `0026_jobs.py` calls `op.create_table("jobs", ...)`, but `0001`'s
  `create_all` already created the `jobs` table from the `Job` ORM
  model, producing a duplicate-table error.
* Several other later revisions (`0017`, `0027`, ...) follow the same
  pattern.

The live production DB only ever worked because each revision ran
forward in actual time, against a DB that matched its predecessor's
shape — but that history could not be replayed for a new dev box, CI,
or the integration test DB.

The fix was a one-time squash: the modern schema is materialised by
`0001` (still via `create_all`), and these historical migrations are
preserved here for archaeology only. See the docstring at the top of
`../0001_initial_schema.py` for the prod-stamping incantation needed
on any DB whose `alembic_version` row currently points at one of these
revisions.
