"""Optional per-bot OAuth connections; importing this package needs only stdlib."""

from .catalog import Operation, Provider, builtin_providers
from .errors import ConnectionError
from .manager import ConnectionManager, OAuthClient, from_env
from .store import ConnectionStore, Vault
from .subscriptions import SubscriptionManager

__all__ = [
    "ConnectionError",
    "ConnectionManager",
    "ConnectionStore",
    "OAuthClient",
    "Operation",
    "Provider",
    "SubscriptionManager",
    "Vault",
    "builtin_providers",
    "from_env",
]
