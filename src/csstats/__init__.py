# Intentionally empty. Under `python -m csstats`, this file executes BEFORE
# __main__.py — any import here runs before gevent's monkey.patch_all() and
# silently breaks it. Do not add re-exports, version strings, or convenience
# imports. See DESIGN.md 2026-08-10-H.