"""
Shared mutable global state that must be available without circular imports.
"""
APP_REVISION: str | None = None


def _load_revision() -> str | None:
    import os
    rev = os.environ.get("APP_REVISION")
    if rev:
        return rev
    try:
        with open("/app/REVISION") as f:
            return f.read().strip()
    except OSError:
        pass
    return None