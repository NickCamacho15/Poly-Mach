"""
Ed25519 authentication for Polymarket US API.

This module handles request signing using Ed25519 signatures as required
by the Polymarket US API specification.
"""

import time
import base64
from typing import Dict, Optional

from cryptography.hazmat.primitives.asymmetric import ed25519


class AuthenticationError(Exception):
    """Raised when authentication fails."""
    pass


class PolymarketAuth:
    """
    Handles Ed25519 authentication for Polymarket US API requests.
    
    The API requires three authentication headers:
    - X-PM-Access-Key: Your API key UUID
    - X-PM-Timestamp: Unix timestamp in milliseconds
    - X-PM-Signature: Base64-encoded Ed25519 signature
    
    Signature is computed over: timestamp + method + path
    
    Example:
        >>> auth = PolymarketAuth(
        ...     api_key_id="your-api-key-uuid",
        ...     private_key_base64="your-base64-private-key"
        ... )
        >>> headers = auth.sign_request("GET", "/v1/account/balances")
    """
    
    def __init__(self, api_key_id: str, private_key_base64: str):
        """
        Initialize authentication handler.
        
        Args:
            api_key_id: Your API key UUID from polymarket.us/developer
            private_key_base64: Base64-encoded Ed25519 private key (32 bytes)
            
        Raises:
            AuthenticationError: If private key is invalid or cannot be decoded
        """
        if not api_key_id:
            raise AuthenticationError("API key ID is required")
        if not private_key_base64:
            raise AuthenticationError("Private key is required")
            
        self.api_key_id = api_key_id
        
        try:
            # Decode base64 private key and use first 32 bytes
            private_key_bytes = base64.b64decode(private_key_base64)
            if len(private_key_bytes) < 32:
                raise AuthenticationError(
                    f"Private key too short: {len(private_key_bytes)} bytes, need 32"
                )
            
            self._private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
                private_key_bytes[:32]
            )
        except Exception as e:
            if isinstance(e, AuthenticationError):
                raise
            raise AuthenticationError(f"Failed to load private key: {e}")
    
    def _get_timestamp(self) -> str:
        """
        Get current timestamp in milliseconds.
        
        Returns:
            Unix timestamp as string (milliseconds)
        """
        return str(int(time.time() * 1000))
    
    def _sign_message(self, message: str) -> str:
        """
        Sign a message with Ed25519 private key.
        
        Args:
            message: The message to sign
            
        Returns:
            Base64-encoded signature
        """
        signature_bytes = self._private_key.sign(message.encode("utf-8"))
        return base64.b64encode(signature_bytes).decode("utf-8")
    
    def sign_request(
        self,
        method: str,
        path: str,
        timestamp: Optional[str] = None
    ) -> Dict[str, str]:
        """
        Generate authentication headers for an API request.
        
        The signature is computed over: timestamp + method + path
        
        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            path: Request path starting with / (e.g., "/v1/orders")
            timestamp: Optional timestamp override (for testing)
            
        Returns:
            Dictionary of headers to include in the request:
            - X-PM-Access-Key
            - X-PM-Timestamp
            - X-PM-Signature
            - Content-Type
            
        Example:
            >>> headers = auth.sign_request("GET", "/v1/account/balances")
            >>> response = requests.get(url, headers=headers)
        """
        if timestamp is None:
            timestamp = self._get_timestamp()
        
        # Construct message: timestamp + method + path
        message = f"{timestamp}{method.upper()}{path}"
        
        # Sign the message
        signature = self._sign_message(message)
        
        return {
            "X-PM-Access-Key": self.api_key_id,
            "X-PM-Timestamp": timestamp,
            "X-PM-Signature": signature,
            "Content-Type": "application/json",
        }
    
    def get_ws_headers(self, path: str) -> Dict[str, str]:
        """
        Generate headers for WebSocket connection.
        
        WebSocket authentication uses the same signature scheme but
        always uses GET method.
        
        Args:
            path: WebSocket path (e.g., "/v1/ws/markets")
            
        Returns:
            Dictionary of headers for WebSocket handshake
        """
        return self.sign_request("GET", path)
    
    def get_public_key(self) -> bytes:
        """
        Get the public key corresponding to the private key.
        
        Useful for debugging and verification.
        
        Returns:
            Raw public key bytes (32 bytes)
        """
        return self._private_key.public_key().public_bytes_raw()
    
    def get_public_key_base64(self) -> str:
        """
        Get the public key as base64 string.
        
        Returns:
            Base64-encoded public key
        """
        return base64.b64encode(self.get_public_key()).decode("utf-8")
