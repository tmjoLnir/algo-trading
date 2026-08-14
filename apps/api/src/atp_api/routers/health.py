"""Liveness and readiness probes."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: is the process up? Must not touch the DB — a slow database
    should not cause the orchestrator to kill a healthy API."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict[str, object]:
    """Readiness: DB, Redis and broker reachable. Fail this to be removed from
    the load balancer without being restarted."""
    raise NotImplementedError
