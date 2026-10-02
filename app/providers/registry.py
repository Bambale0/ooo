from app.providers.argolink import ArgoLinkAdapter
from app.providers.asale import AsaleAdapter
from app.providers.base import ProviderAdapter


def get_provider_adapter(provider: str, *, api_key: str | None = None) -> ProviderAdapter:
    if provider == "argolink":
        return ArgoLinkAdapter(api_key=api_key)
    if provider == "asale":
        return AsaleAdapter(api_key=api_key)
    raise ValueError(f"Unsupported provider: {provider}")
