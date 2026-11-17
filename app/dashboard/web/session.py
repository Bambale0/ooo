"""Helper to create session tokens."""

import hmac
import hashlib
import json
import base64
from typing import Optional


def create_session_token(partner_id: str) -> str:
    """Create a session token for web cabinet."""
    from app.infrastructure.config import get_settings
    
    settings = get_settings()
    
    # Simple JWT-like token (not using external JWT library to reduce dependencies)
    payload = {
        "partner_id": partner_id,
        "type": "web_session"
    }
    
    # Encode
    payload_json = json.dumps(payload)
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode().rstrip("=")
    
    # Sign
    signature = hmac.new(
        settings.secret_key.encode(),
        payload_b64.encode(),
        hashlib.sha256
    ).digest()
    signature_b64 = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    
    return f"{payload_b64}.{signature_b64}"


def verify_session_token(token: str) -> Optional[str]:
    """Verify session token and return partner_id."""
    from app.infrastructure.config import get_settings
    
    settings = get_settings()
    
    try:
        payload_b64, signature_b64 = token.split(".")
        
        # Verify signature
        expected_signature = hmac.new(
            settings.secret_key.encode(),
            payload_b64.encode(),
            hashlib.sha256
        ).digest()
        expected_signature_b64 = base64.urlsafe_b64encode(expected_signature).decode().rstrip("=")
        
        if signature_b64 != expected_signature_b64:
            return None
        
        # Decode payload
        payload_json = base64.urlsafe_b64decode(payload_b64 + "==")
        payload = json.loads(payload_json)
        
        return payload.get("partner_id")
    except Exception:
        return None
