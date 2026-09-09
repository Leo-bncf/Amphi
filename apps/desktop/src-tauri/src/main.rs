// Amphi — application de bureau.
//
// La fenêtre affiche l'interface web déjà existante, sans la modifier. Ce qui
// change par rapport au navigateur, c'est ce qu'on peut faire derrière : la
// transcription tourne en natif, à vitesse native, sur la machine de chaque
// étudiant. C'est la raison d'être de cette app — le navigateur plafonne à un
// vingtième de la vitesse du moteur natif.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod whisper;

use base64::Engine;
use serde::Serialize;
use tauri::Emitter;

#[derive(Serialize)]
struct HostInfo {
    platform: &'static str,
    arch: &'static str,
    /// L'interface s'en sert pour choisir : transcrire ici, ou envoyer au
    /// serveur. Elle ne suppose jamais que l'app sait tout faire.
    native_asr: bool,
    model_present: bool,
    model_size_mb: u64,
}

#[tauri::command]
fn host_info() -> HostInfo {
    let model = whisper::model_state();
    HostInfo {
        platform: std::env::consts::OS,
        arch: std::env::consts::ARCH,
        native_asr: true,
        model_present: model.present,
        model_size_mb: model.size_mb,
    }
}

/// Télécharge le modèle en émettant l'avancement — 574 Mo, il faut le montrer.
#[tauri::command]
async fn download_model(app: tauri::AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let mut last = 0u64;
        whisper::download_model(|done, total| {
            let pct = if total > 0 { done * 100 / total } else { 0 };
            if pct != last {
                last = pct;
                let _ = app.emit("model-progress", pct);
            }
        })
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Transcrit du PCM 16 bits mono 16 kHz, encodé en base64.
///
/// Le navigateur décode lui-même l'enregistrement avec `AudioContext` : c'est
/// gratuit, déjà présent, et ça évite d'embarquer ffmpeg dans l'application.
#[tauri::command]
async fn transcribe_native(
    pcm_base64: String,
    language: Option<String>,
    prompt: Option<String>,
) -> Result<whisper::TranscriptOut, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(pcm_base64.as_bytes())
            .map_err(|e| format!("audio illisible : {e}"))?;
        if bytes.len() % 2 != 0 {
            return Err("flux PCM tronqué".into());
        }
        let samples: Vec<i16> = bytes
            .chunks_exact(2)
            .map(|c| i16::from_le_bytes([c[0], c[1]]))
            .collect();
        whisper::transcribe(&samples, language.as_deref(), prompt.as_deref())
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Adresse du serveur de la promo.
///
/// Lue dans `~/Library/Application Support/Amphi/server.txt` (ou l'équivalent
/// selon la plateforme), sinon la variable d'environnement, sinon le serveur
/// local. Un fichier plutôt qu'une valeur compilée : chaque étudiant pointe la
/// carte sans qu'on lui recompile une application.
/// Mot de passe partagé du serveur, lu à côté de l'adresse.
fn server_password() -> String {
    if let Ok(v) = std::env::var("AMPHI_PASSWORD") {
        if !v.trim().is_empty() {
            return v.trim().to_string();
        }
    }
    dirs_config()
        .and_then(|d| std::fs::read_to_string(d.join("password.txt")).ok())
        .map(|t| t.trim().to_string())
        .unwrap_or_default()
}

fn server_url() -> String {
    if let Ok(from_env) = std::env::var("AMPHI_API") {
        if !from_env.trim().is_empty() {
            return from_env.trim().to_string();
        }
    }
    if let Some(dir) = dirs_config() {
        if let Ok(text) = std::fs::read_to_string(dir.join("server.txt")) {
            let trimmed = text.trim().to_string();
            if !trimmed.is_empty() {
                return trimmed;
            }
        }
    }
    "http://127.0.0.1:8765".to_string()
}

fn dirs_config() -> Option<std::path::PathBuf> {
    let home = std::env::var_os("HOME")?;
    let base = std::path::PathBuf::from(home);
    let dir = if cfg!(target_os = "macos") {
        base.join("Library/Application Support/Amphi")
    } else {
        base.join(".config/amphi")
    };
    std::fs::create_dir_all(&dir).ok()?;
    Some(dir)
}

fn main() {
    let api = server_url();
    // Injecté avant tout script de la page : l'interface lit `AMPHI_API` au
    // chargement pour savoir où adresser ses requêtes.
    let password = server_password();
    let bootstrap = format!(
        "globalThis.AMPHI_API = {}; globalThis.AMPHI_AUTH = {};",
        serde_json::to_string(&api).unwrap(),
        serde_json::to_string(&password).unwrap()
    );

    tauri::Builder::default()
        .append_invoke_initialization_script(bootstrap)
        .setup(move |_app| {
            println!("Amphi — serveur : {api}");
            println!("Amphi — mot de passe : {}", if password.is_empty() { "aucun" } else { "configuré" });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![host_info, download_model, transcribe_native])
        .run(tauri::generate_context!())
        .expect("démarrage de la fenêtre Amphi");
}
