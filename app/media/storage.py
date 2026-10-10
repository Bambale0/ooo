import os
import tempfile
from pathlib import Path

from app.infrastructure.config import Settings, get_settings


class MediaStorageError(Exception):
    pass


class MediaStorage:
    async def put(
        self,
        *,
        key: str,
        content: bytes,
        content_type: str,
    ) -> str:
        raise NotImplementedError

    async def local_path(self, key: str) -> Path | None:
        return None


class LocalMediaStorage(MediaStorage):
    def __init__(self, root: str) -> None:
        self.root = Path(root)

    async def put(
        self,
        *,
        key: str,
        content: bytes,
        content_type: str,
    ) -> str:
        path = self._safe_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
        return key

    def _safe_path(self, key: str) -> Path:
        root = self.root.resolve()
        candidate = (root / key).resolve()
        if not candidate.is_relative_to(root) or candidate == root:
            raise MediaStorageError("Invalid storage key")
        return candidate

    async def local_path(self, key: str) -> Path | None:
        path = self._safe_path(key)
        return path if path.is_file() else None


class S3MediaStorage(MediaStorage):
    def __init__(self, settings: Settings) -> None:
        if not settings.s3_bucket:
            raise MediaStorageError("S3_BUCKET is required")
        if not settings.s3_access_key_id or not settings.s3_secret_access_key:
            raise MediaStorageError("S3 credentials are required")
        try:
            import boto3
        except ImportError as exc:
            raise MediaStorageError("boto3 is required for s3 media storage") from exc
        self.bucket = settings.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region_name,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
        )

    async def put(
        self,
        *,
        key: str,
        content: bytes,
        content_type: str,
    ) -> str:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content,
            ContentType=content_type,
            CacheControl="private, max-age=86400",
        )
        return key


def get_media_storage() -> MediaStorage:
    settings = get_settings()
    if settings.media_storage_backend == "local":
        return LocalMediaStorage(settings.media_local_storage_dir)
    if settings.media_storage_backend == "s3":
        return S3MediaStorage(settings)
    raise MediaStorageError(f"Unsupported media storage backend: {settings.media_storage_backend}")