class FileOperationError(Exception):
    """Raised when file operations fail"""

    pass


class BinaryFileError(FileOperationError):
    """Raised when a text-only file operation is attempted on a binary file.

    Used by FileService.read_file / read_file_content to fail-fast on PDFs,
    images, etc. instead of crashing with UnicodeDecodeError. Callers that
    legitimately handle binary files (e.g. resource_router) should use
    read_file_bytes, or catch this exception and skip the file.
    """

    def __init__(self, path: str):
        self.path = path
        super().__init__(f"Refusing to read binary file as text: {path}")


class EntityNotFoundError(Exception):
    """Raised when an entity cannot be found"""

    pass


class EntityCreationError(Exception):
    """Raised when an entity cannot be created"""

    pass


class EntityAlreadyExistsError(EntityCreationError):
    """Raised when an entity file already exists"""

    pass


class DirectoryOperationError(Exception):
    """Raised when directory operations fail"""

    pass


class SyncFatalError(Exception):
    """Raised when sync encounters a fatal error that prevents continuation.

    Fatal errors include:
    - Project deleted during sync (FOREIGN KEY constraint)
    - Database corruption
    - Critical system failures

    When this exception is raised, the entire sync operation should be terminated
    immediately rather than attempting to continue with remaining files.
    """

    pass
