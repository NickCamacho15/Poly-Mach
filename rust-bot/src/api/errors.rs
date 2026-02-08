//! API error types for Polymarket US client.

#![allow(dead_code)]

use thiserror::Error;

#[derive(Error, Debug)]
pub enum ApiError {
    #[error("HTTP error: {status_code} - {message}")]
    Http {
        status_code: u16,
        error_code: String,
        message: String,
    },

    #[error("Rate limited (retry after {retry_after}s)")]
    RateLimited { retry_after: u64 },

    #[error("Insufficient balance: {0}")]
    InsufficientBalance(String),

    #[error("Market closed: {0}")]
    MarketClosed(String),

    #[error("Invalid order: {0}")]
    InvalidOrder(String),

    #[error("Authentication error: {0}")]
    Authentication(String),

    #[error("Network error: {0}")]
    Network(String),

    #[error("Timeout: {0}")]
    Timeout(String),

    #[error("Deserialization error: {0}")]
    Deserialization(String),

    #[error("Request failed after {attempts} attempts: {last_error}")]
    MaxRetriesExceeded { attempts: u32, last_error: String },
}

impl ApiError {
    /// Parse error from API response JSON.
    pub fn from_response(status_code: u16, body: &str) -> Self {
        // Try to parse structured error response.
        if let Ok(json) = serde_json::from_str::<serde_json::Value>(body) {
            let error = json.get("error").unwrap_or(&json);
            let code = error
                .get("code")
                .and_then(|v| v.as_str())
                .unwrap_or("UNKNOWN")
                .to_string();
            let message = error
                .get("message")
                .and_then(|v| v.as_str())
                .unwrap_or(body)
                .to_string();

            return match code.as_str() {
                "INSUFFICIENT_BALANCE" => Self::InsufficientBalance(message),
                "MARKET_CLOSED" => Self::MarketClosed(message),
                "INVALID_PRICE" | "INVALID_QUANTITY" => Self::InvalidOrder(message),
                "RATE_LIMITED" => Self::RateLimited { retry_after: 1 },
                _ => Self::Http {
                    status_code,
                    error_code: code,
                    message,
                },
            };
        }

        Self::Http {
            status_code,
            error_code: "UNKNOWN".to_string(),
            message: body.to_string(),
        }
    }

    /// Whether this error is retryable.
    pub fn is_retryable(&self) -> bool {
        matches!(
            self,
            Self::RateLimited { .. }
                | Self::Network(_)
                | Self::Timeout(_)
                | Self::Http {
                    status_code: 500..=599,
                    ..
                }
        )
    }
}
