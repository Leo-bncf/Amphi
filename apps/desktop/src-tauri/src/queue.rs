//! Durable, offline-safe contribution upload queue.
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{fs, path::{Path, PathBuf}, time::{SystemTime, UNIX_EPOCH}};

const MAX_ATTEMPTS: u32 = 5;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Envelope {
    pub idempotency_key: String,
    pub endpoint: String,
    pub payload: serde_json::Value,
    pub audio_path: Option<String>,
    #[serde(default)]
    pub audio_endpoint: Option<String>,
    pub attempts: u32,
    pub next_attempt_at_ms: u64,
}

fn now_ms() -> u64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64 }
fn queue_dir() -> Result<PathBuf, String> {
    let home = std::env::var_os("HOME").ok_or("HOME absent")?;
    let dir = PathBuf::from(home).join(if cfg!(target_os = "macos") { "Library/Application Support/Amphi/queue" } else { ".local/share/amphi/queue" });
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    Ok(dir)
}
fn key(endpoint: &str, payload: &serde_json::Value, audio: Option<&str>) -> String {
    let mut h = Sha256::new();
    h.update(endpoint.as_bytes()); h.update([0]);
    h.update(serde_json::to_vec(payload).unwrap_or_default()); h.update([0]);
    if let Some(p) = audio { h.update(p.as_bytes()); }
    hex::encode(h.finalize())
}
fn path_for(dir: &Path, id: &str) -> PathBuf { dir.join(format!("{id}.json")) }

pub fn enqueue(endpoint: String, payload: serde_json::Value, audio_path: Option<String>, audio_endpoint: Option<String>) -> Result<Envelope, String> {
    let dir = queue_dir()?;
    let id = key(&endpoint, &payload, audio_path.as_deref());
    let path = path_for(&dir, &id);
    if let Ok(bytes) = fs::read(&path) { return serde_json::from_slice(&bytes).map_err(|e| e.to_string()); }
    let item = Envelope { idempotency_key: id, endpoint, payload, audio_path, audio_endpoint, attempts: 0, next_attempt_at_ms: 0 };
    persist(&path, &item)?;
    Ok(item)
}
pub fn acknowledge(idempotency_key: String) -> Result<(), String> {
    let path = path_for(&queue_dir()?, &idempotency_key);
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e.to_string()),
    }
}

fn persist(path: &Path, item: &Envelope) -> Result<(), String> {
    let tmp = path.with_extension("tmp");
    fs::write(&tmp, serde_json::to_vec_pretty(item).map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
    fs::rename(tmp, path).map_err(|e| e.to_string())
}

/// Attempts each due item once. Successful (including duplicate/409) items are acknowledged and removed.
pub fn flush(token: Option<&str>, auth: Option<&str>) -> Result<usize, String> {
    let dir = queue_dir()?; let mut acknowledged = 0;
    for entry in fs::read_dir(&dir).map_err(|e| e.to_string())? {
        let path = entry.map_err(|e| e.to_string())?.path();
        if path.extension().and_then(|x| x.to_str()) != Some("json") { continue; }
        let mut item: Envelope = match serde_json::from_slice(&fs::read(&path).map_err(|e| e.to_string())?) { Ok(v) => v, Err(_) => continue };
        if item.next_attempt_at_ms > now_ms() { continue; }
        if item.attempts >= MAX_ATTEMPTS { continue; }
        // Audio is staged separately so a crash or offline period cannot lose it.
        // Replay uploads it first, then contributes the returned durable URL.
        if let (Some(audio_path), Some(audio_endpoint)) = (&item.audio_path, &item.audio_endpoint) {
            let bytes = match fs::read(audio_path) {
                Ok(bytes) => bytes,
                Err(_) => {
                    item.attempts += 1;
                    item.next_attempt_at_ms = now_ms() + 1_000u64.saturating_mul(2u64.saturating_pow(item.attempts.min(6)));
                    persist(&path, &item)?;
                    continue;
                }
            };
            let mut audio_request = ureq::post(audio_endpoint)
                .set("Content-Type", "application/octet-stream");
            if let Some(value) = token.filter(|s| !s.is_empty()) {
                audio_request = audio_request.set("Authorization", &format!("Bearer {value}"));
            } else if let Some(secret) = auth.filter(|s| !s.is_empty()) {
                audio_request = audio_request.set("Authorization", &format!("Basic {}", base64::Engine::encode(&base64::engine::general_purpose::STANDARD, format!("amphi:{secret}"))));
            }
            let upload = audio_request.send_bytes(&bytes);
            match upload {
                Ok(response) => {
                    let uploaded: serde_json::Value = serde_json::from_str(
                        &response.into_string().map_err(|e| e.to_string())?
                    ).map_err(|e| e.to_string())?;
                    if let Some(url) = uploaded.get("url").and_then(|v| v.as_str()) {
                        if let Some(recording) = item.payload.get_mut("payload") {
                            if let Some(obj) = recording.as_object_mut() { obj.insert("url".into(), serde_json::Value::String(url.into())); }
                        }
                    }
                }
                Err(ureq::Error::Status(code, _)) if (400..500).contains(&code) && code != 429 => {
                    fs::remove_file(&path).map_err(|e| e.to_string())?; acknowledged += 1; continue;
                }
                Err(_) => { item.attempts += 1; item.next_attempt_at_ms = now_ms() + 1_000u64.saturating_mul(2u64.saturating_pow(item.attempts.min(6))); persist(&path, &item)?; continue; }
            }
        }
        let mut request = ureq::post(&item.endpoint)
            .set("Content-Type", "application/json")
            .set("Idempotency-Key", &item.idempotency_key);
        if let Some(value) = token.filter(|s| !s.is_empty()) {
            request = request.set("Authorization", &format!("Bearer {value}"));
        } else if let Some(secret) = auth.filter(|s| !s.is_empty()) {
            request = request.set("Authorization", &format!("Basic {}", base64::Engine::encode(&base64::engine::general_purpose::STANDARD, format!("amphi:{secret}"))));
        }
        let result = request
            .send_string(&serde_json::to_string(&item.payload).map_err(|e| e.to_string())?);
        match result {
            Ok(_) => {
                fs::remove_file(&path).map_err(|e| e.to_string())?;
                if let Some(audio_path) = &item.audio_path { let _ = fs::remove_file(audio_path); }
                acknowledged += 1;
            }
            Err(ureq::Error::Status(code, _)) if (400..500).contains(&code) && code != 429 => {
                // A server-side duplicate or permanent validation result cannot be fixed by retrying.
                fs::remove_file(&path).map_err(|e| e.to_string())?; acknowledged += 1;
            }
            Err(_) => {
                item.attempts += 1;
                item.next_attempt_at_ms = now_ms() + 1_000u64.saturating_mul(2u64.saturating_pow(item.attempts.min(6)));
                persist(&path, &item)?;
            }
        }
    }
    Ok(acknowledged)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test] fn key_is_stable_and_changes_with_payload() {
        let a = serde_json::json!({"x":1});
        assert_eq!(key("u", &a, None), key("u", &a, None));
        assert_ne!(key("u", &a, None), key("u", &serde_json::json!({"x":2}), None));
    }
    #[test] fn envelope_roundtrips() {
        let e = Envelope { idempotency_key: "k".into(), endpoint: "u".into(), payload: serde_json::json!({"x":1}), audio_path: Some("/tmp/a".into()), audio_endpoint: None, attempts: 0, next_attempt_at_ms: 0 };
        assert_eq!(e, serde_json::from_str(&serde_json::to_string(&e).unwrap()).unwrap());
    }
}
