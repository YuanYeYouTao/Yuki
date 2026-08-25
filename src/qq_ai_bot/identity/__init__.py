"""Canonical identity persistence, C7 backfill, and C8 v1 dual-write.

Schema modules remain persistence metadata. Backfill is an offline sqlite3
tool. Runtime dual-write lives in dual_write and joins the caller's
AsyncSession. Control-plane is a later commit.
"""
