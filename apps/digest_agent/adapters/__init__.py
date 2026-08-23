"""Infrastructure adapters for the Digest Agent."""

from .delivery import FakeDeliveryAdapter, TermuxNotificationDeliveryAdapter
from .provider import FakeDigestProvider
from .search import BraveSearchClient, FakeSearchClient, SearchAdapterError
from .sqlite import SQLiteDigestRepository
from .workspace import WorkspaceArtifactClient

__all__ = [
    "BraveSearchClient", "FakeDeliveryAdapter", "FakeDigestProvider",
    "FakeSearchClient", "SearchAdapterError",
    "SQLiteDigestRepository", "TermuxNotificationDeliveryAdapter",
    "WorkspaceArtifactClient",
]
