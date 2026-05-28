//! RSS leak PoC: 模拟 Python backend 关键 alloc 模式
//! - 每 2s GET gamma /events?live=true (跟 discovery_runner.py 同模式)
//! - 持续 1 WSS 订阅 (跟 market_ws_client.py 同模式)
//! 用 rustls (绕 OpenSSL) + mimalloc (绕 macOS libc) 验证是否 RSS 稳定

use mimalloc::MiMalloc;
use std::time::Duration;

#[global_allocator]
static GLOBAL: MiMalloc = MiMalloc;

const GAMMA_URL: &str = "https://gamma-api.polymarket.com/events?limit=50&active=true&closed=false";
const WSS_URL: &str = "wss://ws-subscriptions-clob.polymarket.com/ws/market";

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    println!("rss_leak_poc start pid={} (rustls + mimalloc)", std::process::id());

    let client = reqwest::Client::builder()
        .http2_prior_knowledge()
        .pool_max_idle_per_host(5)
        .pool_idle_timeout(Duration::from_secs(5))
        .build()?;

    // WSS task (持长连接)
    tokio::spawn(async move {
        use futures_util::{StreamExt, SinkExt};
        loop {
            match tokio_tungstenite::connect_async(WSS_URL).await {
                Ok((mut ws, _)) => {
                    // 订阅一些 active market token_id (用 dummy id, 服务端可能拒)
                    let sub = serde_json::json!({"type":"market","assets_ids":["dummy"]});
                    let _ = ws.send(tokio_tungstenite::tungstenite::Message::Text(sub.to_string().into())).await;
                    while let Some(Ok(_)) = ws.next().await {
                        // 持续收消息
                    }
                }
                Err(e) => {
                    eprintln!("WSS connect err: {e}, retry in 5s");
                    tokio::time::sleep(Duration::from_secs(5)).await;
                }
            }
        }
    });

    // REST poll loop
    let mut tick = 0u64;
    let mut interval = tokio::time::interval(Duration::from_secs(2));
    loop {
        interval.tick().await;
        tick += 1;
        let t0 = std::time::Instant::now();
        match client.get(GAMMA_URL).send().await {
            Ok(resp) => {
                let bytes = resp.bytes().await?;
                let n = bytes.len();
                let parsed: Result<serde_json::Value, _> = serde_json::from_slice(&bytes);
                let count = parsed.ok()
                    .and_then(|v| v.as_array().map(|a| a.len()))
                    .unwrap_or(0);
                let ms = t0.elapsed().as_millis();
                if tick % 15 == 0 {
                    // 30s log 一次
                    println!("tick={tick} events={count} bytes={n} ms={ms}");
                }
            }
            Err(e) => eprintln!("GET err: {e}"),
        }
    }
}
