"""Deterministic side-effect tools.

Tools in this package perform *controlled writes*. They are not agents: they call no
model, make no judgement, and decide nothing about authorization, impact, workflow
state, or completion. Each one receives an already-qualified request from the
deterministic boundary and either performs exactly the authorized change or fails
closed with zero effect.

Import discipline matches the rest of the distribution — stdlib, pydantic, and
``driftzero`` only, enforced by the M0 purity guard.
"""
