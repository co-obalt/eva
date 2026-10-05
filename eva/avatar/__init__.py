"""AVATAR companion bridge (EVA_VRM_INTEGRATION_PLAN.md): event projector only,
never a source of intelligence. A dead bus can never block or crash EVA core."""
from .bus import AvatarBus

__all__ = ["AvatarBus"]
