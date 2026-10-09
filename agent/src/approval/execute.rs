//! Spending an approval: `POST {mcp}/approvals/execute`.
//!
//! The signed token goes straight from here to the MCP server. Nothing
//! model-visible ever carries it, and the request body holds nothing but the
//! token and the request id the token is bound to — every mutation parameter
//! is read by the server from the signed claims.

use std::time::Duration;

use serde::Deserialize;
use serde_json::{Value, json};

/// The MCP endpoint that applies an approved mutation, derived from the MCP
/// URL exactly as on `main`, so there is nothing extra to configure.
pub fn execute_url(mcp_url: &str) -> String {
    let base = mcp_url.trim_end_matches('/');
    let base = base.strip_suffix("/mcp").unwrap_or(base);
    format!("{base}/approvals/execute")
}

/// What the MCP server reported. `committed: false` is a normal outcome: a
/// legitimately approved change can still be refused by backend policy.
#[derive(Debug, Clone, PartialEq)]
pub struct ExecuteOutcome {
    pub committed: bool,
    pub result: Value,
}

#[derive(Deserialize)]
struct ExecuteResponse {
    ok: bool,
    #[serde(default)]
    result: Value,
}

pub async fn apply(
    http: &reqwest::Client,
    mcp_url: &str,
    mcp_api_key: &str,
    token: &str,
    request_id: &str,
) -> ExecuteOutcome {
    let response = http
        .post(execute_url(mcp_url))
        .bearer_auth(mcp_api_key)
        .timeout(Duration::from_secs(30))
        .json(&json!({ "approval_token": token, "request_id": request_id }))
        .send()
        .await;
    let response = match response {
        Ok(response) => response,
        Err(error) => {
            // The reqwest error can name internal hosts; it goes to the log,
            // not to the model.
            tracing::error!(error = %error.without_url(), "the approval execution endpoint was unreachable");
            return ExecuteOutcome {
                committed: false,
                result: json!({ "error": "the approved change could not be applied" }),
            };
        }
    };
    let status = response.status();
    match response.json::<ExecuteResponse>().await {
        // 200 with ok:true or ok:false, or 403 with ok:false and a reason.
        Ok(body) => ExecuteOutcome { committed: body.ok && status.is_success(), result: body.result },
        Err(_) => ExecuteOutcome {
            committed: false,
            result: json!({ "error": format!("the approval execution endpoint answered {status}") }),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_execution_url_is_derived_from_the_mcp_url() {
        assert_eq!(execute_url("http://mcp-server:8080/mcp"), "http://mcp-server:8080/approvals/execute");
        assert_eq!(execute_url("http://mcp-server:8080/mcp/"), "http://mcp-server:8080/approvals/execute");
        assert_eq!(execute_url("http://127.0.0.1:9/"), "http://127.0.0.1:9/approvals/execute");
    }
}
