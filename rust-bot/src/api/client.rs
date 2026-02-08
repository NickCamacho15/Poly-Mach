//! Async REST API client for Polymarket US.
//!
//! Features:
//! - Ed25519 authentication
//! - Rate limiting (configurable, default 10 req/sec)
//! - Automatic retries with exponential backoff
//! - Typed responses
//!
//! Ported from Python `src/api/client.py`.

use governor::{Quota, RateLimiter};
use reqwest::Client;
use rust_decimal::Decimal;
use std::num::NonZeroU32;
use std::sync::Arc;
use std::time::Duration;
use tracing::{debug, warn};

use crate::auth::PolymarketAuth;
use crate::data::models::*;
use crate::data::orderbook::parse_book_side;

use super::errors::ApiError;

/// Async REST client for Polymarket US.
pub struct PolymarketClient {
    auth: PolymarketAuth,
    base_url: String,
    client: Client,
    rate_limiter: Arc<RateLimiter<governor::state::NotKeyed, governor::state::InMemoryState, governor::clock::DefaultClock>>,
    max_retries: u32,
}

impl PolymarketClient {
    pub fn new(
        auth: PolymarketAuth,
        base_url: &str,
        rate_limit: u32,
        max_retries: u32,
        timeout_secs: u64,
    ) -> Result<Self, ApiError> {
        let client = Client::builder()
            .timeout(Duration::from_secs(timeout_secs))
            .pool_max_idle_per_host(20)
            .tcp_keepalive(Duration::from_secs(30))
            .build()
            .map_err(|e| ApiError::Network(e.to_string()))?;

        let quota = Quota::per_second(NonZeroU32::new(rate_limit).unwrap_or(NonZeroU32::new(10).unwrap()));
        let rate_limiter = Arc::new(RateLimiter::direct(quota));

        Ok(Self {
            auth,
            base_url: base_url.trim_end_matches('/').to_string(),
            client,
            rate_limiter,
            max_retries,
        })
    }

    /// Create with default settings.
    pub fn with_defaults(auth: PolymarketAuth, base_url: &str) -> Result<Self, ApiError> {
        Self::new(auth, base_url, 10, 3, 30)
    }

    // =========================================================================
    // Core request method
    // =========================================================================

    async fn request(
        &self,
        method: reqwest::Method,
        path: &str,
        body: Option<&serde_json::Value>,
        params: Option<&[(&str, &str)],>,
    ) -> Result<serde_json::Value, ApiError> {
        let url = format!("{}{}", self.base_url, path);
        let mut last_error: Option<ApiError> = None;

        for attempt in 0..self.max_retries {
            // Rate limiting
            self.rate_limiter.until_ready().await;

            // Generate fresh auth headers for each attempt
            let auth_headers = self.auth.sign_request(method.as_str(), path);

            debug!(
                method = %method,
                path = %path,
                attempt = attempt + 1,
                "API request"
            );

            let mut req = self
                .client
                .request(method.clone(), &url)
                .headers(auth_headers.to_header_map());

            if let Some(body) = body {
                req = req.json(body);
            }

            if let Some(params) = params {
                req = req.query(params);
            }

            let result = req.send().await;

            match result {
                Ok(response) => {
                    let status = response.status();

                    if status.is_success() {
                        let text = response
                            .text()
                            .await
                            .map_err(|e| ApiError::Network(e.to_string()))?;
                        let json: serde_json::Value = serde_json::from_str(&text)
                            .map_err(|e| ApiError::Deserialization(e.to_string()))?;
                        return Ok(json);
                    }

                    // Rate limit — always retry
                    if status.as_u16() == 429 {
                        let retry_after = response
                            .headers()
                            .get("Retry-After")
                            .and_then(|v| v.to_str().ok())
                            .and_then(|v| v.parse::<u64>().ok())
                            .unwrap_or(1);
                        warn!(retry_after, attempt = attempt + 1, "Rate limited");
                        tokio::time::sleep(Duration::from_secs(retry_after)).await;
                        last_error = Some(ApiError::RateLimited { retry_after });
                        continue;
                    }

                    // Server errors — retry with backoff
                    if status.as_u16() >= 500 {
                        let delay_ms = (500 * 2u64.pow(attempt)) as u64;
                        warn!(
                            status_code = status.as_u16(),
                            delay_ms,
                            attempt = attempt + 1,
                            "Server error, retrying"
                        );
                        tokio::time::sleep(Duration::from_millis(delay_ms)).await;
                        last_error = Some(ApiError::Http {
                            status_code: status.as_u16(),
                            error_code: "SERVER_ERROR".to_string(),
                            message: status.to_string(),
                        });
                        continue;
                    }

                    // Client errors — don't retry
                    let body_text = response.text().await.unwrap_or_default();
                    return Err(ApiError::from_response(status.as_u16(), &body_text));
                }
                Err(e) => {
                    let delay_ms = (500 * 2u64.pow(attempt)) as u64;
                    warn!(
                        error = %e,
                        delay_ms,
                        attempt = attempt + 1,
                        "Network error, retrying"
                    );
                    tokio::time::sleep(Duration::from_millis(delay_ms)).await;

                    if e.is_timeout() {
                        last_error = Some(ApiError::Timeout(e.to_string()));
                    } else {
                        last_error = Some(ApiError::Network(e.to_string()));
                    }
                    continue;
                }
            }
        }

        Err(last_error.unwrap_or_else(|| ApiError::MaxRetriesExceeded {
            attempts: self.max_retries,
            last_error: "Unknown error".to_string(),
        }))
    }

    // =========================================================================
    // Account Endpoints
    // =========================================================================

    /// Get account balance.
    pub async fn get_balance(&self) -> Result<Balance, ApiError> {
        let data = self
            .request(reqwest::Method::GET, "/v1/account/balances", None, None)
            .await?;

        // Handle array format: {"balances": [...]}
        if let Some(balances) = data.get("balances").and_then(|v| v.as_array()) {
            // Prefer USD entry.
            let entry = balances
                .iter()
                .find(|b| {
                    b.get("currency")
                        .and_then(|c| c.as_str())
                        .map(|c| c.eq_ignore_ascii_case("USD"))
                        .unwrap_or(false)
                })
                .or_else(|| balances.first());

            if let Some(entry) = entry {
                return serde_json::from_value(entry.clone())
                    .map_err(|e| ApiError::Deserialization(e.to_string()));
            }
        }

        // Fallback: flat object.
        serde_json::from_value(data).map_err(|e| ApiError::Deserialization(e.to_string()))
    }

    // =========================================================================
    // Portfolio Endpoints
    // =========================================================================

    /// Get all current positions.
    pub async fn get_positions(&self) -> Result<Vec<Position>, ApiError> {
        let data = self
            .request(reqwest::Method::GET, "/v1/portfolio/positions", None, None)
            .await?;

        // Try multiple schemas (matching Python's resilient parsing).
        let raw = data
            .get("positions")
            .or_else(|| data.get("data").and_then(|d| d.get("positions")))
            .or_else(|| data.get("portfolio").and_then(|d| d.get("positions")))
            .or_else(|| data.get("availablePositions"));

        let entries = match raw {
            Some(serde_json::Value::Array(arr)) => arr.clone(),
            Some(serde_json::Value::Object(map)) => {
                // Map format: {slug -> position}
                map.values().cloned().collect()
            }
            _ => {
                warn!("Could not parse positions response");
                return Ok(Vec::new());
            }
        };

        let mut positions = Vec::new();
        for entry in entries {
            // Handle nested {"position": {...}} wrapper.
            let pos_val = entry
                .get("position")
                .filter(|v| v.is_object())
                .unwrap_or(&entry);

            match serde_json::from_value::<Position>(pos_val.clone()) {
                Ok(pos) => positions.push(pos),
                Err(e) => {
                    warn!(error = %e, "Failed to parse position");
                }
            }
        }

        Ok(positions)
    }

    // =========================================================================
    // Order Endpoints
    // =========================================================================

    /// Create a new order.
    pub async fn create_order(&self, order: &OrderRequest) -> Result<CreateOrderResponse, ApiError> {
        let body = serde_json::to_value(order)
            .map_err(|e| ApiError::Deserialization(e.to_string()))?;

        let data = self
            .request(reqwest::Method::POST, "/v1/orders", Some(&body), None)
            .await?;

        serde_json::from_value(data).map_err(|e| ApiError::Deserialization(e.to_string()))
    }

    /// Preview an order before submitting.
    pub async fn preview_order(&self, order: &OrderRequest) -> Result<OrderPreview, ApiError> {
        let body = serde_json::to_value(order)
            .map_err(|e| ApiError::Deserialization(e.to_string()))?;

        let data = self
            .request(reqwest::Method::POST, "/v1/order/preview", Some(&body), None)
            .await?;

        let preview_val = data.get("estimatedFill").unwrap_or(&data);
        serde_json::from_value(preview_val.clone())
            .map_err(|e| ApiError::Deserialization(e.to_string()))
    }

    /// Get all open orders.
    pub async fn get_open_orders(
        &self,
        market_slug: Option<&str>,
    ) -> Result<Vec<Order>, ApiError> {
        let params: Vec<(&str, &str)> = market_slug
            .map(|s| vec![("marketSlug", s)])
            .unwrap_or_default();

        let data = self
            .request(
                reqwest::Method::GET,
                "/v1/orders/open",
                None,
                if params.is_empty() { None } else { Some(&params) },
            )
            .await?;

        let orders = data
            .get("orders")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();

        orders
            .into_iter()
            .map(|o| serde_json::from_value(o).map_err(|e| ApiError::Deserialization(e.to_string())))
            .collect()
    }

    /// Get order details by ID.
    pub async fn get_order(&self, order_id: &str) -> Result<Order, ApiError> {
        let path = format!("/v1/order/{}", order_id);
        let data = self.request(reqwest::Method::GET, &path, None, None).await?;
        serde_json::from_value(data).map_err(|e| ApiError::Deserialization(e.to_string()))
    }

    /// Cancel a specific order.
    pub async fn cancel_order(&self, order_id: &str) -> Result<Order, ApiError> {
        let path = format!("/v1/order/{}/cancel", order_id);
        let data = self.request(reqwest::Method::POST, &path, None, None).await?;
        serde_json::from_value(data).map_err(|e| ApiError::Deserialization(e.to_string()))
    }

    /// Cancel all open orders.
    pub async fn cancel_all_orders(
        &self,
        market_slug: Option<&str>,
    ) -> Result<serde_json::Value, ApiError> {
        let params: Vec<(&str, &str)> = market_slug
            .map(|s| vec![("marketSlug", s)])
            .unwrap_or_default();

        self.request(
            reqwest::Method::POST,
            "/v1/orders/open/cancel",
            None,
            if params.is_empty() { None } else { Some(&params) },
        )
        .await
    }

    /// Modify an existing order.
    pub async fn modify_order(
        &self,
        order_id: &str,
        price: Option<Decimal>,
        quantity: Option<i64>,
    ) -> Result<Order, ApiError> {
        let mut payload = serde_json::Map::new();
        if let Some(p) = price {
            payload.insert(
                "price".to_string(),
                serde_json::json!({"value": p.to_string(), "currency": "USD"}),
            );
        }
        if let Some(q) = quantity {
            payload.insert("quantity".to_string(), serde_json::json!(q));
        }

        let path = format!("/v1/order/{}/modify", order_id);
        let body = serde_json::Value::Object(payload);
        let data = self.request(reqwest::Method::POST, &path, Some(&body), None).await?;
        serde_json::from_value(data).map_err(|e| ApiError::Deserialization(e.to_string()))
    }

    /// Close entire position in a market.
    pub async fn close_position(&self, market_slug: &str) -> Result<serde_json::Value, ApiError> {
        let body = serde_json::json!({"marketSlug": market_slug});
        self.request(
            reqwest::Method::POST,
            "/v1/order/close-position",
            Some(&body),
            None,
        )
        .await
    }

    // =========================================================================
    // Market Endpoints
    // =========================================================================

    /// Get list of available markets.
    ///
    /// Pass `closed: Some("false")` to only get open (non-closed) markets,
    /// matching the Python bot's `discover_markets()` behavior.
    pub async fn get_markets(
        &self,
        status: Option<&str>,
        category: Option<&str>,
        limit: u32,
        offset: u32,
        closed: Option<&str>,
    ) -> Result<Vec<Market>, ApiError> {
        let mut params = vec![
            ("limit", limit.to_string()),
            ("offset", offset.to_string()),
        ];
        if let Some(s) = status {
            params.push(("status", s.to_string()));
        }
        if let Some(c) = category {
            params.push(("category", c.to_string()));
        }
        if let Some(cl) = closed {
            params.push(("closed", cl.to_string()));
        }

        let param_refs: Vec<(&str, &str)> = params.iter().map(|(k, v)| (*k, v.as_str())).collect();

        let data = self
            .request(
                reqwest::Method::GET,
                "/v1/markets",
                None,
                Some(&param_refs),
            )
            .await?;

        let markets = data
            .get("markets")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();

        // Log the first raw market to see ALL available API fields.
        if let Some(first) = markets.first() {
            tracing::info!(raw = %first, "Raw listing market (all fields)");
        }

        // Parse each market individually; skip any that fail deserialization.
        let parsed: Vec<Market> = markets
            .into_iter()
            .filter_map(|m| {
                match serde_json::from_value::<Market>(m.clone()) {
                    Ok(market) => Some(market),
                    Err(e) => {
                        tracing::debug!(
                            error = %e,
                            "Skipping unparseable market"
                        );
                        None
                    }
                }
            })
            .collect();

        Ok(parsed)
    }

    /// Get market details by slug.
    pub async fn get_market(&self, market_slug: &str) -> Result<Market, ApiError> {
        let path = format!("/v1/market/{}", market_slug);
        let data = self.request(reqwest::Method::GET, &path, None, None).await?;
        serde_json::from_value(data).map_err(|e| ApiError::Deserialization(e.to_string()))
    }

    /// Get raw (untyped) market details for debugging.
    pub async fn get_market_raw(&self, market_slug: &str) -> Result<serde_json::Value, ApiError> {
        let path = format!("/v1/market/{}", market_slug);
        self.request(reqwest::Method::GET, &path, None, None).await
    }

    /// Raw GET request for endpoint probing/debugging.
    pub async fn request_raw(&self, path: &str) -> Result<serde_json::Value, ApiError> {
        self.request(reqwest::Method::GET, path, None, None).await
    }

    /// Get order book for a market.
    pub async fn get_market_sides(&self, market_slug: &str) -> Result<OrderBook, ApiError> {
        let path = format!("/v1/market/{}/sides", market_slug);
        let data = self.request(reqwest::Method::GET, &path, None, None).await?;

        let yes_side = data
            .get("yes")
            .map(|v| parse_book_side(v))
            .unwrap_or_default();
        let no_side = data
            .get("no")
            .map(|v| parse_book_side(v))
            .unwrap_or_default();

        Ok(OrderBook {
            market_slug: data
                .get("marketSlug")
                .and_then(|v| v.as_str())
                .unwrap_or(market_slug)
                .to_string(),
            yes: yes_side,
            no: no_side,
        })
    }
}
