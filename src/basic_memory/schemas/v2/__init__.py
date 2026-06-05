"""V2 API schemas - ID-based entity and project references."""

from basic_memory.schemas.v2.entity import (
    EntityResolveRequest,
    EntityResolveResponse,
    EntityResponseV2,
    MoveEntityRequestV2,
    MoveDirectoryRequestV2,
    DeleteDirectoryRequestV2,
    ProjectResolveRequest,
    ProjectResolveResponse,
)
from basic_memory.schemas.v2.bulk_edit import (
    BulkEditItemResult,
    BulkEditOperation,
    BulkEditRequest,
    BulkEditResponse,
)
from basic_memory.schemas.v2.graph import (
    GraphEdge,
    GraphNode,
    GraphResponse,
    OrphanEntitiesResponse,
)
from basic_memory.schemas.v2.resource import (
    CreateResourceRequest,
    UpdateResourceRequest,
    ResourceResponse,
)

__all__ = [
    "BulkEditItemResult",
    "BulkEditOperation",
    "BulkEditRequest",
    "BulkEditResponse",
    "EntityResolveRequest",
    "EntityResolveResponse",
    "EntityResponseV2",
    "MoveEntityRequestV2",
    "MoveDirectoryRequestV2",
    "DeleteDirectoryRequestV2",
    "ProjectResolveRequest",
    "ProjectResolveResponse",
    "GraphEdge",
    "GraphNode",
    "GraphResponse",
    "OrphanEntitiesResponse",
    "CreateResourceRequest",
    "UpdateResourceRequest",
    "ResourceResponse",
]
