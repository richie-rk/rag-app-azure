"""The SQL data-access layer (`01-repository.md` section 6.2; ADR 0026): `engine` (the only way to
SQL), `statements` (the only module that calls `sqlalchemy.text`), `tables` (the Core metadata),
`settings` (`SqlSettings`) and `telemetry` (the `docchat.sql.*` instruments).

The package imports none of them itself, so a tool that needs only the metadata (the migrations)
doesn't load the driver.
"""
