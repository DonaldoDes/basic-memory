"""V2 Knowledge Router - External ID-based entity operations.

This router provides external_id (UUID) based CRUD operations for entities,
using stable string UUIDs that won't change with file moves or database migrations.

Key improvements:
- Stable external UUIDs that won't change with file moves or renames
- Better API ergonomics with consistent string identifiers
- Direct database lookups via unique indexed column
- Simplified caching strategies
"""

from pathlib import Path as FilePath

from fastapi import APIRouter, BackgroundTasks, HTTPException, Response, Path
from loguru import logger

import logfire
from basic_memory.deps import (
    EntityServiceV2ExternalDep,
    SearchServiceV2ExternalDep,
    LinkResolverV2ExternalDep,
    ProjectRepositoryDep,
    ProjectConfigV2ExternalDep,
    AppConfigDep,
    EntityRepositoryV2ExternalDep,
    RelationRepositoryV2ExternalDep,
    ProjectExternalIdPathDep,
    FileServiceV2ExternalDep,
    TaskSchedulerDep,
)
from basic_memory.schemas import DeleteEntitiesResponse
from basic_memory.schemas.base import Entity
from basic_memory.schemas.request import EditEntityRequest
from basic_memory.schemas.v2 import (
    BulkEditItemResult,
    BulkEditRequest,
    BulkEditResponse,
    EntityResolveRequest,
    EntityResolveResponse,
    EntityResponseV2,
    GraphEdge,
    GraphNode,
    GraphResponse,
    MoveEntityRequestV2,
    MoveDirectoryRequestV2,
    DeleteDirectoryRequestV2,
    OrphanEntitiesResponse,
)
from basic_memory.schemas.v2.bulk_edit import is_path_traversal_identifier
from basic_memory.services.exceptions import BinaryFileError, EntityNotFoundError
from basic_memory.schemas.response import DirectoryMoveResult, DirectoryDeleteResult
from basic_memory.utils import build_permalink_resolution_candidates
from basic_memory.workspace_context import current_workspace_permalink_context

router = APIRouter(prefix="/knowledge", tags=["knowledge-v2"])


def _schedule_vector_sync_if_enabled(
    *,
    task_scheduler,
    app_config,
    entity_id: int,
    project_id: int,
) -> None:
    """Schedule out-of-band vector sync only when semantic search is enabled."""
    if app_config.semantic_search_enabled:
        task_scheduler.schedule(
            "sync_entity_vectors",
            entity_id=entity_id,
            project_id=project_id,
        )


## Graph endpoint


@router.get("/graph", response_model=GraphResponse)
async def get_graph(
    project_id: ProjectExternalIdPathDep,
    entity_repository: EntityRepositoryV2ExternalDep,
    relation_repository: RelationRepositoryV2ExternalDep,
) -> GraphResponse:
    """Return all entities and resolved relations for knowledge graph visualization.

    Returns a flat node/edge structure optimized for rendering with graph libraries.
    Only includes resolved relations (where to_id is not null).
    """
    with logfire.span(
        "api.request.knowledge.get_graph",
        entrypoint="api",
        domain="knowledge",
        action="get_graph",
    ):
        logger.info("API v2 request: get_graph")

        # Fetch all entities for this project
        entities = await entity_repository.find_all(use_load_options=False)
        nodes = [
            GraphNode(
                external_id=entity.external_id,
                title=entity.title,
                note_type=entity.note_type,
                file_path=entity.file_path,
            )
            for entity in entities
        ]

        # Fetch all resolved relations (to_id is not null) with eager-loaded entities
        relations = await relation_repository.find_all()
        edges = [
            GraphEdge(
                from_id=relation.from_entity.external_id,
                to_id=relation.to_entity.external_id,
                relation_type=relation.relation_type,
            )
            for relation in relations
            if relation.to_entity is not None
        ]

        logger.info(f"API v2 response: graph with {len(nodes)} nodes and {len(edges)} edges")
        return GraphResponse(nodes=nodes, edges=edges)


## Orphan entities endpoint


@router.get("/orphans", response_model=OrphanEntitiesResponse)
async def get_orphan_entities(
    project_id: ProjectExternalIdPathDep,
    entity_repository: EntityRepositoryV2ExternalDep,
) -> OrphanEntitiesResponse:
    """Return entities that have no incoming or outgoing relations."""
    with logfire.span(
        "api.request.knowledge.get_orphans",
        entrypoint="api",
        domain="knowledge",
        action="get_orphans",
    ):
        logger.info("API v2 request: get_orphan_entities")

        entities = await entity_repository.find_without_relations()
        nodes = [
            GraphNode(
                external_id=entity.external_id,
                title=entity.title,
                note_type=entity.note_type,
                file_path=entity.file_path,
            )
            for entity in entities
        ]

        logger.info(f"API v2 response: {len(nodes)} orphan entities")
        return OrphanEntitiesResponse(entities=nodes, total=len(nodes))


## Resolution endpoint


@router.post("/resolve", response_model=EntityResolveResponse)
async def resolve_identifier(
    project_id: ProjectExternalIdPathDep,
    data: EntityResolveRequest,
    link_resolver: LinkResolverV2ExternalDep,
    entity_repository: EntityRepositoryV2ExternalDep,
    project_repository: ProjectRepositoryDep,
) -> EntityResolveResponse:
    """Resolve a string identifier (external_id, permalink, title, or path) to entity info.

    This endpoint provides a bridge between v1-style identifiers and v2 external_ids.
    Use this to convert existing references to the new UUID-based format.

    Args:
        data: Request containing the identifier to resolve

    Returns:
        Entity external_id and metadata about how it was resolved

    Raises:
        HTTPException: 404 if identifier cannot be resolved

    Example:
        POST /v2/{project_id}/knowledge/resolve
        {"identifier": "specs/search"}

        Returns:
        {
            "external_id": "550e8400-e29b-41d4-a716-446655440000",
            "entity_id": 123,
            "project_external_id": "4b9b7a10-7a63-48d2-ae3f-0d6a2c69313f",
            "permalink": "specs/search",
            "file_path": "specs/search.md",
            "title": "Search Specification",
            "resolution_method": "permalink"
        }
    """
    with logfire.span(
        "api.request.knowledge.resolve_entity",
        entrypoint="api",
        domain="knowledge",
        action="resolve_entity",
    ):
        logger.info(f"API v2 request: resolve_identifier for '{data.identifier}'")

        entity = await entity_repository.get_by_external_id(data.identifier)
        resolution_method = "external_id" if entity else "search"

        if not entity:
            entity = await link_resolver.resolve_link(
                data.identifier, source_path=data.source_path, strict=data.strict
            )
            if entity:
                if entity.permalink == data.identifier:
                    resolution_method = "permalink"
                elif entity.title == data.identifier:
                    resolution_method = "title"
                elif entity.file_path == data.identifier:
                    resolution_method = "path"
                else:
                    resolution_method = "search"

        if not entity:
            raise HTTPException(status_code=404, detail=f"Entity not found: '{data.identifier}'")

        owner_project = await project_repository.get_by_id(entity.project_id)
        if not owner_project:  # pragma: no cover
            raise HTTPException(
                status_code=500,
                detail="Resolved entity references an unknown project",
            )

        result = EntityResolveResponse(
            external_id=entity.external_id,
            entity_id=entity.id,
            project_external_id=owner_project.external_id,
            permalink=entity.permalink,
            file_path=entity.file_path,
            title=entity.title,
            resolution_method=resolution_method,
        )

        logger.debug(
            f"API v2 response: resolved '{data.identifier}' to external_id={result.external_id} via {resolution_method}"
        )

        return result


## Read endpoints


@router.get("/entities/dataview")
async def list_entities_for_dataview(
    project_id: ProjectExternalIdPathDep,
    entity_repository: EntityRepositoryV2ExternalDep,
    file_service: FileServiceV2ExternalDep,
) -> list[dict]:
    """List all entities in a format suitable for Dataview query execution.

    Returns entities with file metadata and frontmatter fields needed by Dataview:
    - file.path, file.name, file.folder
    - title
    - type (entity_type)
    - permalink (optional)
    - All frontmatter fields from the source file

    This endpoint is used by build_context to provide notes to the Dataview integration.

    Args:
        project_id: Project external ID from URL path

    Returns:
        List of note dictionaries with Dataview-compatible structure
    """
    from pathlib import Path as PathLib
    import frontmatter

    logger.info(f"API v2 request: list_entities_for_dataview for project {project_id}")

    # Get all entities in the project
    all_entities = await entity_repository.find_all()

    notes = []
    for entity in all_entities:
        # Convert entity to note format expected by Dataview
        note = {
            "file": {
                "path": entity.file_path,
                "name": PathLib(entity.file_path).name,
                "folder": str(PathLib(entity.file_path).parent),
            },
            "title": entity.title,
            "type": entity.note_type,
        }

        # Add permalink if available
        if entity.permalink:
            note["permalink"] = entity.permalink

        # Add entity_metadata as frontmatter for Dataview field resolution
        if entity.entity_metadata:
            note.update(entity.entity_metadata)

        # Try to load additional frontmatter from the file
        try:
            file_content, _ = await file_service.read_file(entity.file_path)
            post = frontmatter.loads(file_content)
            if post.metadata:
                # Add all frontmatter fields to the note (don't overwrite existing)
                for key, value in post.metadata.items():
                    if key not in note:
                        note[key] = value
        except BinaryFileError:
            # Non-markdown files (PDF, images, …) have no frontmatter to read.
            # Skip silently — entity metadata from DB is still included above.
            logger.debug(
                f"Skipping frontmatter load for binary entity {entity.permalink} "
                f"(file_path={entity.file_path})"
            )
        except Exception as ex:
            logger.debug(f"Could not load frontmatter for {entity.permalink}: {ex}")

        notes.append(note)

    logger.info(f"API v2 response: list_entities_for_dataview returned {len(notes)} notes")

    return notes


@router.get("/entities/{entity_id}", response_model=EntityResponseV2)
async def get_entity_by_id(
    project_id: ProjectExternalIdPathDep,
    entity_repository: EntityRepositoryV2ExternalDep,
    entity_id: str = Path(..., description="Entity external ID (UUID)"),
) -> EntityResponseV2:
    """Get an entity by its external ID (UUID).

    This is the primary entity retrieval method in v2, using stable UUID
    identifiers that won't change with file moves.

    Args:
        entity_id: External ID (UUID string)

    Returns:
        Complete entity with observations and relations

    Raises:
        HTTPException: 404 if entity not found
    """
    with logfire.span(
        "api.request.knowledge.get_entity",
        entrypoint="api",
        domain="knowledge",
        action="get_entity",
    ):
        logger.info(f"API v2 request: get_entity_by_id entity_id={entity_id}")

        entity = await entity_repository.get_by_external_id(entity_id)
        if not entity:
            raise HTTPException(
                status_code=404, detail=f"Entity with external_id '{entity_id}' not found"
            )

        result = EntityResponseV2.model_validate(entity)
        logger.info(f"API v2 response: external_id={entity_id}, title='{result.title}'")

        return result


## Create endpoints


@router.post("/entities", response_model=EntityResponseV2)
async def create_entity(
    project_id: ProjectExternalIdPathDep,
    data: Entity,
    entity_service: EntityServiceV2ExternalDep,
    search_service: SearchServiceV2ExternalDep,
    task_scheduler: TaskSchedulerDep,
    app_config: AppConfigDep,
) -> EntityResponseV2:
    """Create a new entity.

    Args:
        data: Entity data to create

    Returns:
        Created entity with generated external_id (UUID) and file content
    """
    with logfire.span(
        "api.request.knowledge.create_entity",
        entrypoint="api",
        domain="knowledge",
        action="create_entity",
    ):
        logger.info(
            "API v2 request", endpoint="create_entity", note_type=data.note_type, title=data.title
        )

        # Note writes are now internally consistent before the response returns. We only leave
        # truly derived work, like semantic vectors, on the async scheduler.
        write_result = await entity_service.create_entity_with_content(data)
        entity = write_result.entity
        await search_service.index_entity(entity, content=write_result.search_content)
        _schedule_vector_sync_if_enabled(
            task_scheduler=task_scheduler,
            app_config=app_config,
            entity_id=entity.id,
            project_id=project_id,
        )

        result = EntityResponseV2.model_validate(entity)
        # The write service already returns the canonical markdown accepted for this request.
        result = result.model_copy(update={"content": write_result.content})

        logger.info(
            f"API v2 response: endpoint='create_entity' external_id={entity.external_id}, title={result.title}, permalink={result.permalink}, status_code=201"
        )
        return result


## Update endpoints


@router.put("/entities/{entity_id}", response_model=EntityResponseV2)
async def update_entity_by_id(
    data: Entity,
    response: Response,
    project_id: ProjectExternalIdPathDep,
    entity_service: EntityServiceV2ExternalDep,
    search_service: SearchServiceV2ExternalDep,
    entity_repository: EntityRepositoryV2ExternalDep,
    task_scheduler: TaskSchedulerDep,
    app_config: AppConfigDep,
    entity_id: str = Path(..., description="Entity external ID (UUID)"),
) -> EntityResponseV2:
    """Update an entity by external ID.

    If the entity doesn't exist, it will be created (upsert behavior).

    Args:
        entity_id: External ID (UUID string)
        data: Updated entity data

    Returns:
        Updated entity with file content
    """
    with logfire.span(
        "api.request.knowledge.update_entity",
        entrypoint="api",
        domain="knowledge",
        action="update_entity",
    ):
        logger.info(f"API v2 request: update_entity_by_id entity_id={entity_id}")

        existing = await entity_repository.get_by_external_id(entity_id)
        created = existing is None

        if existing:
            write_result = await entity_service.update_entity_with_content(existing, data)
            entity = write_result.entity
            response.status_code = 200
        else:
            write_result = await entity_service.create_entity_with_content(data)
            entity = write_result.entity
            if entity.external_id != entity_id:
                entity = await entity_repository.update(
                    entity.id,
                    {"external_id": entity_id},
                )
                # external_id fixup only changes the DB row. The file content is unchanged,
                # so the markdown captured during the write remains valid downstream.
                if not entity:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Entity with external_id '{entity_id}' not found",
                    )
            response.status_code = 201

        await search_service.index_entity(entity, content=write_result.search_content)
        _schedule_vector_sync_if_enabled(
            task_scheduler=task_scheduler,
            app_config=app_config,
            entity_id=entity.id,
            project_id=project_id,
        )

        result = EntityResponseV2.model_validate(entity)
        result = result.model_copy(update={"content": write_result.content})

        logger.info(
            f"API v2 response: external_id={entity_id}, created={created}, status_code={response.status_code}"
        )
        return result


@router.patch("/entities/{entity_id}", response_model=EntityResponseV2)
async def edit_entity_by_id(
    data: EditEntityRequest,
    project_id: ProjectExternalIdPathDep,
    entity_service: EntityServiceV2ExternalDep,
    search_service: SearchServiceV2ExternalDep,
    entity_repository: EntityRepositoryV2ExternalDep,
    task_scheduler: TaskSchedulerDep,
    app_config: AppConfigDep,
    entity_id: str = Path(..., description="Entity external ID (UUID)"),
) -> EntityResponseV2:
    """Edit an existing entity by external ID using operations like append, prepend, etc.

    Args:
        entity_id: External ID (UUID string)
        data: Edit operation details

    Returns:
        Updated entity with file content

    Raises:
        HTTPException: 404 if entity not found, 400 if edit fails
    """
    with logfire.span(
        "api.request.knowledge.edit_entity",
        entrypoint="api",
        domain="knowledge",
        action="edit_entity",
    ):
        logger.info(
            f"API v2 request: edit_entity_by_id entity_id={entity_id}, operation='{data.operation}'"
        )

        entity = await entity_repository.get_by_external_id(entity_id)
        if not entity:  # pragma: no cover
            raise HTTPException(
                status_code=404, detail=f"Entity with external_id '{entity_id}' not found"
            )

        try:
            identifier = entity.permalink or entity.file_path
            write_result = await entity_service.edit_entity_with_content(
                identifier=identifier,
                operation=data.operation,
                content=data.content,
                section=data.section,
                find_text=data.find_text,
                expected_replacements=data.expected_replacements,
            )
            updated_entity = write_result.entity
            await search_service.index_entity(updated_entity, content=write_result.search_content)
            _schedule_vector_sync_if_enabled(
                task_scheduler=task_scheduler,
                app_config=app_config,
                entity_id=updated_entity.id,
                project_id=project_id,
            )

            result = EntityResponseV2.model_validate(updated_entity)
            result = result.model_copy(update={"content": write_result.content})

            logger.info(
                f"API v2 response: external_id={entity_id}, operation='{data.operation}', status_code=200"
            )

            return result

        except Exception as e:
            logger.error(f"Error editing entity {entity_id}: {e}")
            raise HTTPException(status_code=400, detail=str(e))


## Delete endpoints


@router.delete("/entities/{entity_id}", response_model=DeleteEntitiesResponse)
async def delete_entity_by_id(
    project_id: ProjectExternalIdPathDep,
    entity_service: EntityServiceV2ExternalDep,
    entity_repository: EntityRepositoryV2ExternalDep,
    entity_id: str = Path(..., description="Entity external ID (UUID)"),
) -> DeleteEntitiesResponse:
    """Delete an entity by external ID.

    Args:
        entity_id: External ID (UUID string)

    Returns:
        Deletion status

    Note: Returns deleted=False if entity doesn't exist (idempotent)
    """
    with logfire.span(
        "api.request.knowledge.delete_entity",
        entrypoint="api",
        domain="knowledge",
        action="delete_entity",
    ):
        logger.info(f"API v2 request: delete_entity_by_id entity_id={entity_id}")

        entity = await entity_repository.get_by_external_id(entity_id)
        if entity is None:
            logger.info(f"API v2 response: external_id={entity_id} not found, deleted=False")
            return DeleteEntitiesResponse(deleted=False)

        # Delete the entity using internal ID
        deleted = await entity_service.delete_entity(entity.id)

        logger.info(f"API v2 response: external_id={entity_id}, deleted={deleted}")

        return DeleteEntitiesResponse(deleted=deleted)


## Move endpoint


@router.put("/entities/{entity_id}/move", response_model=EntityResponseV2)
async def move_entity(
    data: MoveEntityRequestV2,
    project_id: ProjectExternalIdPathDep,
    entity_service: EntityServiceV2ExternalDep,
    entity_repository: EntityRepositoryV2ExternalDep,
    project_config: ProjectConfigV2ExternalDep,
    app_config: AppConfigDep,
    search_service: SearchServiceV2ExternalDep,
    task_scheduler: TaskSchedulerDep,
    entity_id: str = Path(..., description="Entity external ID (UUID)"),
) -> EntityResponseV2:
    """Move an entity to a new file location.

    V2 API uses external_id (UUID) in the URL path for stable references.
    The external_id will remain stable after the move.

    Args:
        project_id: Project external ID from URL path
        entity_id: Entity external ID from URL path (primary identifier)
        data: Move request with destination path only

    Returns:
        Updated entity with new file path
    """
    with logfire.span(
        "api.request.knowledge.move_entity",
        entrypoint="api",
        domain="knowledge",
        action="move_entity",
    ):
        logger.info(
            f"API v2 request: move_entity entity_id={entity_id}, destination='{data.destination_path}'"
        )

        try:
            # First, get the entity by external_id to verify it exists
            entity = await entity_repository.get_by_external_id(entity_id)
            if not entity:  # pragma: no cover
                raise HTTPException(
                    status_code=404, detail=f"Entity with external_id '{entity_id}' not found"
                )

            # Move the entity using its current file path as identifier
            moved_entity = await entity_service.move_entity(
                identifier=entity.file_path,  # Use file path for resolution
                destination_path=data.destination_path,
                project_config=project_config,
                app_config=app_config,
            )

            # Reindex at new location
            reindexed_entity = await entity_service.link_resolver.resolve_link(
                data.destination_path
            )
            if reindexed_entity:
                await search_service.index_entity(reindexed_entity)
                _schedule_vector_sync_if_enabled(
                    task_scheduler=task_scheduler,
                    app_config=app_config,
                    entity_id=reindexed_entity.id,
                    project_id=project_id,
                )

            result = EntityResponseV2.model_validate(moved_entity)

            logger.info(
                f"API v2 response: moved external_id={entity_id} to '{data.destination_path}'"
            )

            return result

        except HTTPException:  # pragma: no cover
            raise  # pragma: no cover
        except Exception as e:
            logger.error(f"Error moving entity: {e}")
            raise HTTPException(status_code=400, detail=str(e))


## Move directory endpoint


@router.post("/move-directory", response_model=DirectoryMoveResult)
async def move_directory(
    data: MoveDirectoryRequestV2,
    project_id: ProjectExternalIdPathDep,
    entity_service: EntityServiceV2ExternalDep,
    project_config: ProjectConfigV2ExternalDep,
    app_config: AppConfigDep,
    search_service: SearchServiceV2ExternalDep,
    task_scheduler: TaskSchedulerDep,
) -> DirectoryMoveResult:
    """Move all entities in a directory to a new location.

    V2 API uses project external_id in the URL path for stable references.
    Moves all files within a source directory to a destination directory,
    updating database records and optionally updating permalinks.

    Args:
        project_id: Project external ID from URL path
        data: Move request with source and destination directories

    Returns:
        DirectoryMoveResult with counts and details of moved files
    """
    with logfire.span(
        "api.request.knowledge.move_directory",
        entrypoint="api",
        domain="knowledge",
        action="move_directory",
    ):
        logger.info(
            f"API v2 request: move_directory source='{data.source_directory}', destination='{data.destination_directory}'"
        )

        try:
            # Move the directory using the service
            result = await entity_service.move_directory(
                source_directory=data.source_directory,
                destination_directory=data.destination_directory,
                project_config=project_config,
                app_config=app_config,
            )

            # Reindex moved entities
            for file_path in result.moved_files:
                entity = await entity_service.link_resolver.resolve_link(file_path)
                if entity:
                    await search_service.index_entity(entity)
                    _schedule_vector_sync_if_enabled(
                        task_scheduler=task_scheduler,
                        app_config=app_config,
                        entity_id=entity.id,
                        project_id=project_id,
                    )

            logger.info(
                f"API v2 response: move_directory "
                f"total={result.total_files}, success={result.successful_moves}, failed={result.failed_moves}"
            )
            return result

        except Exception as e:
            logger.error(f"Error moving directory: {e}")
            raise HTTPException(status_code=400, detail=str(e))


## Delete directory endpoint


@router.post("/delete-directory", response_model=DirectoryDeleteResult)
async def delete_directory(
    data: DeleteDirectoryRequestV2,
    project_id: ProjectExternalIdPathDep,
    entity_service: EntityServiceV2ExternalDep,
) -> DirectoryDeleteResult:
    """Delete all entities in a directory.

    V2 API uses project external_id in the URL path for stable references.
    Deletes all files within a directory, updating database records and
    removing files from the filesystem.

    Args:
        project_id: Project external ID from URL path
        data: Delete request with directory path

    Returns:
        DirectoryDeleteResult with counts and details of deleted files
    """
    with logfire.span(
        "api.request.knowledge.delete_directory",
        entrypoint="api",
        domain="knowledge",
        action="delete_directory",
    ):
        logger.info(f"API v2 request: delete_directory directory='{data.directory}'")

        try:
            # Delete the directory using the service
            result = await entity_service.delete_directory(
                directory=data.directory,
            )

            logger.info(
                f"API v2 response: delete_directory "
                f"total={result.total_files}, success={result.successful_deletes}, failed={result.failed_deletes}"
            )
            return result

        except Exception as e:
            logger.error(f"Error deleting directory: {e}")
            raise HTTPException(status_code=400, detail=str(e))


## Bulk edit endpoint (spec: specs/bulk-edit-notes/spec.md)


async def _resolve_bulk_edit_identifier(identifier, entity_repository, permalink_candidates):
    """Resolve a batch item identifier strictly within the project scope.

    Resolution order: permalink candidates (shared canonical builder, same
    compatibility behavior as the single-note path) → exact title (ambiguity
    detected) → file path (with and without .md).

    Deliberately does NOT use LinkResolver: its project-prefix fallback can
    resolve "other-project/note" into another project, which would violate the
    single-project invariant (I-6). Every lookup here goes through the
    project-scoped entity repository only — a candidate string mentioning
    another project can never match a row outside this project.

    Returns:
        (entity, None) on success, (None, error_code) otherwise.
    """
    for candidate in permalink_candidates:
        entity = await entity_repository.get_by_permalink(candidate, load_relations=False)
        if entity:
            return entity, None

    title_matches = await entity_repository.get_by_title(identifier, load_relations=False)
    if len(title_matches) > 1:
        return None, "AMBIGUOUS_IDENTIFIER"
    if len(title_matches) == 1:
        return title_matches[0], None

    entity = await entity_repository.get_by_file_path(identifier, load_relations=False)
    if entity:
        return entity, None
    if not identifier.endswith(".md"):
        entity = await entity_repository.get_by_file_path(f"{identifier}.md", load_relations=False)
        if entity:
            return entity, None

    return None, "NOT_FOUND"


def _map_bulk_edit_error(error: Exception) -> tuple[str, str]:
    """Map an edit failure to a per-item error code (spec § Codes d'erreur)."""
    message = str(error)
    if isinstance(error, EntityNotFoundError):
        return "NOT_FOUND", message
    if "Text to replace not found" in message:
        return "TEXT_NOT_FOUND", message
    if "Expected" in message and "occurrences" in message:
        return "REPLACEMENT_COUNT_MISMATCH", message
    if "Multiple sections found" in message:
        return "DUPLICATE_SECTION", message
    return "UNKNOWN_ERROR", message


@router.post("/entities/bulk-edit", response_model=BulkEditResponse)
async def bulk_edit_entities(
    data: BulkEditRequest,
    background_tasks: BackgroundTasks,
    project_id: ProjectExternalIdPathDep,
    entity_service: EntityServiceV2ExternalDep,
    search_service: SearchServiceV2ExternalDep,
    entity_repository: EntityRepositoryV2ExternalDep,
    file_service: FileServiceV2ExternalDep,
    project_repository: ProjectRepositoryDep,
    task_scheduler: TaskSchedulerDep,
    app_config: AppConfigDep,
) -> BulkEditResponse:
    """Apply up to 100 edit operations in one request (best-effort per note).

    Execution semantics (spec: specs/bulk-edit-notes/spec.md):
    - Items run sequentially in request order; an edit on a note already edited
      in the same batch sees the previous result.
    - A failed item never interrupts the batch unless stop_on_error=true, in
      which case remaining items are reported as skipped (no rollback of
      already-succeeded items).
    - validate_first=true is a PURE dry-run: items are evaluated against a
      projected content map and nothing is written, even when all items pass.
    - FTS indexing of modified notes is deferred to background tasks (C-2) and
      the vector sync is scheduled ONCE for the whole batch.
    - A path-traversal identifier fails its own item with error_code SECURITY
      before any filesystem or repository access (I-1).
    """
    with logfire.span(
        "api.request.knowledge.bulk_edit_entities",
        entrypoint="api",
        domain="knowledge",
        action="bulk_edit_entities",
    ):
        logger.info(
            f"API v2 request: bulk_edit_entities items={len(data.edits)} "
            f"validate_first={data.validate_first} stop_on_error={data.stop_on_error}"
        )

        # Permalink candidates share the canonical builder used by the
        # single-note resolver (legacy project-prefixed forms, workspace forms).
        owner_project = await project_repository.get_by_id(entity_repository.project_id)
        project_permalink = owner_project.permalink if owner_project else None
        workspace_context = current_workspace_permalink_context()
        workspace_permalink = (
            workspace_context.workspace_slug
            if workspace_context and workspace_context.should_prefix_permalinks
            else None
        )

        results: list[BulkEditItemResult] = []
        projected_content: dict[int, str] = {}
        pending_index: dict[int, tuple] = {}
        modified_entity_ids: set[int] = set()
        stop_requested = False

        def record_failure(identifier: str, error_code: str, message: str) -> None:
            nonlocal stop_requested
            results.append(
                BulkEditItemResult(
                    identifier=identifier,
                    status="failed",
                    error=message,
                    error_code=error_code,
                )
            )
            if data.stop_on_error:
                stop_requested = True

        for edit in data.edits:
            if stop_requested:
                results.append(BulkEditItemResult(identifier=edit.identifier, status="skipped"))
                continue

            # I-1: reject traversal identifiers before ANY repository/file access.
            if is_path_traversal_identifier(edit.identifier):
                logger.warning("Bulk edit: path traversal identifier blocked")
                record_failure(
                    edit.identifier,
                    "SECURITY",
                    "Identifier rejected: paths must stay within project boundaries",
                )
                continue

            permalink_candidates = build_permalink_resolution_candidates(
                edit.identifier,
                project_permalink,
                include_project=app_config.permalinks_include_project,
                workspace_permalink=workspace_permalink,
            )
            entity, resolution_error = await _resolve_bulk_edit_identifier(
                edit.identifier, entity_repository, permalink_candidates
            )
            if resolution_error:
                # I-4: never auto-create — an unresolved identifier is a failure.
                message = (
                    f"Entity not found: {edit.identifier}"
                    if resolution_error == "NOT_FOUND"
                    else f"Identifier resolves to multiple entities: {edit.identifier}"
                )
                record_failure(edit.identifier, resolution_error, message)
                continue

            if data.validate_first:
                # Dry-run: replay the pure edit operation on projected content.
                try:
                    base_content = projected_content.get(entity.id)
                    if base_content is None:
                        base_content, _ = await file_service.read_file(FilePath(entity.file_path))
                    projected_content[entity.id] = entity_service.apply_edit_operation(
                        base_content,
                        edit.operation,
                        edit.content,
                        section=edit.section,
                        find_text=edit.find_text,
                        expected_replacements=edit.expected_replacements,
                    )
                    results.append(
                        BulkEditItemResult(
                            identifier=edit.identifier,
                            status="validated",
                            permalink=entity.permalink,
                            file_path=entity.file_path,
                        )
                    )
                except Exception as e:
                    error_code, message = _map_bulk_edit_error(e)
                    record_failure(edit.identifier, error_code, message)
                continue

            try:
                write_result = await entity_service.edit_entity_with_content(
                    identifier=entity.permalink or entity.file_path,
                    operation=edit.operation,
                    content=edit.content,
                    section=edit.section,
                    find_text=edit.find_text,
                    expected_replacements=edit.expected_replacements,
                )
                updated_entity = write_result.entity
                # C-2: defer FTS indexing to background tasks (last write wins
                # for notes edited several times in the same batch).
                pending_index[updated_entity.id] = (updated_entity, write_result.search_content)
                modified_entity_ids.add(updated_entity.id)
                results.append(
                    BulkEditItemResult(
                        identifier=edit.identifier,
                        status="success",
                        permalink=updated_entity.permalink,
                        file_path=updated_entity.file_path,
                        checksum=updated_entity.checksum,
                    )
                )
            except Exception as e:
                error_code, message = _map_bulk_edit_error(e)
                record_failure(edit.identifier, error_code, message)

        for indexed_entity, search_content in pending_index.values():
            background_tasks.add_task(
                search_service.index_entity, indexed_entity, content=search_content
            )

        vector_sync = "disabled"
        if modified_entity_ids and app_config.semantic_search_enabled:
            task_scheduler.schedule(
                "sync_entity_vectors_batch",
                entity_ids=list(modified_entity_ids),
                project_id=project_id,
            )
            vector_sync = "scheduled"

        status_counts = {"success": 0, "failed": 0, "skipped": 0, "validated": 0}
        for item in results:
            status_counts[item.status] += 1

        logger.info(
            f"API v2 response: bulk_edit_entities total={len(results)} "
            f"succeeded={status_counts['success']} failed={status_counts['failed']} "
            f"skipped={status_counts['skipped']} validated={status_counts['validated']} "
            f"vector_sync={vector_sync}"
        )

        return BulkEditResponse(
            total=len(results),
            succeeded=status_counts["success"],
            failed=status_counts["failed"],
            skipped=status_counts["skipped"],
            validated=status_counts["validated"],
            results=results,
            vector_sync=vector_sync,
        )
