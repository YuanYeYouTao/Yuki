"""Memory contracts and the concrete per-turn session.

This subpackage owns how memory participates in one turn: the per-turn
contract (``contract``), orthogonal session machines (``state``), the
concrete session (``turn_session``), trusted scope resolution (``resolver``),
the unified query plane (``query_plane``), mutation outcome mapping
(``command_plane``), deterministic finalization helpers (``finalizer``) and the
capability-facing view derivation (``capability_view``).

Boundary rules: this package must never import ``qq_ai_bot.planner`` nor
``qq_ai_bot.capabilities`` — interaction with the capability runtime happens
exclusively through the pure data view in ``qq_ai_bot.runtime.contracts``.
Import from the submodules directly; this ``__init__`` re-exports nothing.
"""
