"""AI 层：Context / Role / Session / Transport 三件套 + 一个 transport。"""

from .context import Context
from .parser import PARSERS
from .role import Role
from .roles import DEFAULT_ROLES, get_role
from .session import Session, Turn
from .transport import AIError, ProviderError, stream_chat
from .types import AIConfig, Chunk, ChunkKind, Message

__all__ = [
    "AIConfig",
    "AIError",
    "Chunk",
    "ChunkKind",
    "Context",
    "DEFAULT_ROLES",
    "Message",
    "PARSERS",
    "ProviderError",
    "Role",
    "Session",
    "Turn",
    "get_role",
    "stream_chat",
]
