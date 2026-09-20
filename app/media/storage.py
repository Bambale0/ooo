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
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return key

    async def local_path(self, key: str) -> Path | None:
        path = self.root / key
        return path if path.exists() else None


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
