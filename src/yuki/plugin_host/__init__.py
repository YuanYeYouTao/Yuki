"""Trusted in-process Host for the public Yuki Plugin API.

The package initializer intentionally imports no Host modules: migrations and
core startup may import plugin table metadata before optional Host dependencies.
"""
