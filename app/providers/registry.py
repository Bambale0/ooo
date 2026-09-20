from app.providers.argolink import ArgoLinkAdapter
from app.providers.base import ProviderAdapter


def get_provider_adapter(provider: str) -> ProviderAdapter:
    if provider == "argolink":
        return ArgoLinkAdapter()
    raise ValueError(f"Unsupported provider: {provider}")
