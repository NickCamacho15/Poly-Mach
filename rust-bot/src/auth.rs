//! Ed25519 authentication for Polymarket US API.
//!
//! Handles request signing using Ed25519 signatures as required
//! by the Polymarket US API specification. Ported from Python `src/api/auth.py`.
//!
//! The API requires three authentication headers:
//! - X-PM-Access-Key: Your API key UUID
//! - X-PM-Timestamp: Unix timestamp in milliseconds
//! - X-PM-Signature: Base64-encoded Ed25519 signature
//!
//! Signature is computed over: timestamp + method + path

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine;
use ed25519_dalek::{Signer, SigningKey};
use std::time::{SystemTime, UNIX_EPOCH};
use thiserror::Error;

#[derive(Error, Debug)]
pub enum AuthError {
    #[error("API key ID is required")]
    MissingApiKeyId,
    #[error("Private key is required")]
    MissingPrivateKey,
    #[error("Failed to decode private key: {0}")]
    KeyDecodeError(String),
    #[error("Private key too short: {len} bytes, need 32")]
    KeyTooShort { len: usize },
}

/// Authentication headers for a signed request.
#[derive(Debug, Clone)]
pub struct AuthHeaders {
    pub access_key: String,
    pub timestamp: String,
    pub signature: String,
}

impl AuthHeaders {
    /// Convert to reqwest header map.
    pub fn to_header_map(&self) -> reqwest::header::HeaderMap {
        let mut headers = reqwest::header::HeaderMap::new();
        headers.insert(
            "X-PM-Access-Key",
            self.access_key.parse().unwrap(),
        );
        headers.insert(
            "X-PM-Timestamp",
            self.timestamp.parse().unwrap(),
        );
        headers.insert(
            "X-PM-Signature",
            self.signature.parse().unwrap(),
        );
        headers.insert(
            reqwest::header::CONTENT_TYPE,
            "application/json".parse().unwrap(),
        );
        headers
    }
}

/// Ed25519 authenticator for Polymarket US API requests.
#[derive(Clone)]
pub struct PolymarketAuth {
    api_key_id: String,
    signing_key: SigningKey,
}

impl PolymarketAuth {
    /// Create a new authenticator.
    ///
    /// # Arguments
    /// * `api_key_id` - Your API key UUID from polymarket.us/developer
    /// * `private_key_base64` - Base64-encoded Ed25519 private key (32 bytes)
    pub fn new(api_key_id: &str, private_key_base64: &str) -> Result<Self, AuthError> {
        if api_key_id.is_empty() {
            return Err(AuthError::MissingApiKeyId);
        }
        if private_key_base64.is_empty() {
            return Err(AuthError::MissingPrivateKey);
        }

        let key_bytes = BASE64
            .decode(private_key_base64)
            .map_err(|e| AuthError::KeyDecodeError(e.to_string()))?;

        if key_bytes.len() < 32 {
            return Err(AuthError::KeyTooShort {
                len: key_bytes.len(),
            });
        }

        // Use first 32 bytes (matching Python implementation).
        let mut key_array = [0u8; 32];
        key_array.copy_from_slice(&key_bytes[..32]);
        let signing_key = SigningKey::from_bytes(&key_array);

        Ok(Self {
            api_key_id: api_key_id.to_string(),
            signing_key,
        })
    }

    /// Generate authentication headers for an API request.
    ///
    /// The signature is computed over: `timestamp + METHOD + path`
    ///
    /// # Arguments
    /// * `method` - HTTP method (GET, POST, PUT, DELETE)
    /// * `path` - Request path starting with / (e.g., "/v1/orders")
    pub fn sign_request(&self, method: &str, path: &str) -> AuthHeaders {
        let timestamp = Self::get_timestamp();
        self.sign_request_with_timestamp(method, path, &timestamp)
    }

    /// Sign with a specific timestamp (useful for testing).
    pub fn sign_request_with_timestamp(
        &self,
        method: &str,
        path: &str,
        timestamp: &str,
    ) -> AuthHeaders {
        // Construct message: timestamp + method + path
        let message = format!("{}{}{}", timestamp, method.to_uppercase(), path);

        // Sign the message
        let signature = self.signing_key.sign(message.as_bytes());
        let signature_b64 = BASE64.encode(signature.to_bytes());

        AuthHeaders {
            access_key: self.api_key_id.clone(),
            timestamp: timestamp.to_string(),
            signature: signature_b64,
        }
    }

    /// Get current timestamp in milliseconds.
    fn get_timestamp() -> String {
        let duration = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("SystemTime before UNIX EPOCH");
        let millis = duration.as_millis();
        millis.to_string()
    }

    /// Get the public key as base64 (useful for debugging).
    pub fn public_key_base64(&self) -> String {
        let public_key = self.signing_key.verifying_key();
        BASE64.encode(public_key.as_bytes())
    }
}

impl std::fmt::Debug for PolymarketAuth {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PolymarketAuth")
            .field("api_key_id", &self.api_key_id)
            .field("public_key", &self.public_key_base64())
            .finish()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_auth_headers_generated() {
        // Generate a test key
        let key_bytes = [42u8; 32];
        let key_b64 = BASE64.encode(key_bytes);

        let auth = PolymarketAuth::new("test-key-id", &key_b64).unwrap();
        let headers = auth.sign_request_with_timestamp("GET", "/v1/account/balances", "1700000000000");

        assert_eq!(headers.access_key, "test-key-id");
        assert_eq!(headers.timestamp, "1700000000000");
        assert!(!headers.signature.is_empty());
    }

    #[test]
    fn test_signature_deterministic() {
        let key_bytes = [42u8; 32];
        let key_b64 = BASE64.encode(key_bytes);

        let auth = PolymarketAuth::new("test-key-id", &key_b64).unwrap();
        let h1 = auth.sign_request_with_timestamp("GET", "/v1/test", "12345");
        let h2 = auth.sign_request_with_timestamp("GET", "/v1/test", "12345");

        assert_eq!(h1.signature, h2.signature);
    }

    #[test]
    fn test_empty_key_rejected() {
        assert!(PolymarketAuth::new("", "abc").is_err());
        assert!(PolymarketAuth::new("key", "").is_err());
    }

    #[test]
    fn test_short_key_rejected() {
        let short_key = BASE64.encode([1u8; 16]);
        assert!(PolymarketAuth::new("key", &short_key).is_err());
    }
}
