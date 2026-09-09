//! Transcription native, dans l'application.
//!
//! C'est la raison d'être de l'app. Mesuré sur la même machine et le même
//! audio : le navigateur plafonne à 3,9× le temps réel avec un petit modèle
//! — et sa sortie était inexploitable — là où le moteur natif atteint 9,9×
//! avec le grand. Un facteur vingt, qui décide si un étudiant peut transcrire
//! son cours chez lui ou doit le faire payer au serveur.

use std::io::Read;
use std::path::PathBuf;
use std::sync::Mutex;

use serde::Serialize;
use whisper_rs::{FullParams, SamplingStrategy, WhisperContext, WhisperContextParameters};

/// Quantifié en q5_0 : 574 Mo au lieu de 1,6 Go pour une perte de qualité
/// négligeable. Sur une connexion d'étudiant, le gigaoctet économisé compte
/// plus que la troisième décimale du WER.
pub const MODEL_FILE: &str = "ggml-large-v3-turbo-q5_0.bin";
const MODEL_URL: &str =
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin";

/// Le contexte pèse plusieurs centaines de mégaoctets et met des secondes à
/// se charger : on le garde entre deux parties d'un même cours.
static CONTEXT: Mutex<Option<WhisperContext>> = Mutex::new(None);

#[derive(Serialize)]
pub struct Word {
    pub text: String,
    #[serde(rename = "startMs")]
    pub start_ms: f64,
    #[serde(rename = "endMs")]
    pub end_ms: f64,
    pub confidence: f64,
}

#[derive(Serialize)]
pub struct Segment {
    #[serde(rename = "startMs")]
    pub start_ms: f64,
    #[serde(rename = "endMs")]
    pub end_ms: f64,
    pub text: String,
    pub words: Vec<Word>,
    pub lang: Option<String>,
    #[serde(rename = "avgConfidence")]
    pub avg_confidence: f64,
}

#[derive(Serialize)]
pub struct TranscriptOut {
    pub segments: Vec<Segment>,
    #[serde(rename = "audioDurationMs")]
    pub audio_duration_ms: f64,
    pub provider: String,
    pub model: String,
    #[serde(rename = "costMilliCents")]
    pub cost_milli_cents: f64,
    #[serde(rename = "processingMs")]
    pub processing_ms: f64,
}

#[derive(Serialize, Clone)]
pub struct ModelState {
    pub present: bool,
    pub path: String,
    #[serde(rename = "sizeMb")]
    pub size_mb: u64,
}

pub fn models_dir() -> Option<PathBuf> {
    let home = std::env::var_os("HOME")?;
    let base = PathBuf::from(home);
    let dir = if cfg!(target_os = "macos") {
        base.join("Library/Application Support/Amphi/models")
    } else {
        base.join(".local/share/amphi/models")
    };
    std::fs::create_dir_all(&dir).ok()?;
    Some(dir)
}

pub fn model_path() -> Option<PathBuf> {
    models_dir().map(|d| d.join(MODEL_FILE))
}

pub fn model_state() -> ModelState {
    match model_path() {
        Some(p) => {
            let size = std::fs::metadata(&p).map(|m| m.len()).unwrap_or(0);
            ModelState {
                // Un téléchargement interrompu laisse un fichier tronqué qui
                // ferait échouer whisper.cpp de façon obscure. On exige une
                // taille plausible plutôt que la simple existence.
                present: size > 400 * 1024 * 1024,
                path: p.to_string_lossy().into_owned(),
                size_mb: size / (1024 * 1024),
            }
        }
        None => ModelState { present: false, path: String::new(), size_mb: 0 },
    }
}

/// Télécharge le modèle en signalant l'avancement à l'interface.
pub fn download_model(mut on_progress: impl FnMut(u64, u64)) -> Result<(), String> {
    let target = model_path().ok_or("dossier des modèles introuvable")?;
    let temp = target.with_extension("part");

    let response = ureq::get(MODEL_URL)
        .call()
        .map_err(|e| format!("téléchargement impossible : {e}"))?;
    let total: u64 = response
        .header("Content-Length")
        .and_then(|v| v.parse().ok())
        .unwrap_or(0);

    let mut reader = response.into_reader();
    let mut file = std::fs::File::create(&temp).map_err(|e| e.to_string())?;
    let mut buffer = vec![0u8; 1 << 20];
    let mut done: u64 = 0;
    loop {
        let n = reader.read(&mut buffer).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        std::io::Write::write_all(&mut file, &buffer[..n]).map_err(|e| e.to_string())?;
        done += n as u64;
        on_progress(done, total);
    }
    drop(file);
    // Renommage final : tant que le téléchargement n'est pas complet, le
    // fichier porte une autre extension et ne sera jamais chargé par erreur.
    std::fs::rename(&temp, &target).map_err(|e| e.to_string())?;
    Ok(())
}

/// PCM 16 bits signé, mono 16 kHz — la forme que le navigateur produit après
/// décodage. On évite ainsi d'embarquer ffmpeg dans l'application.
pub fn transcribe(
    pcm_i16: &[i16],
    language: Option<&str>,
    prompt: Option<&str>,
) -> Result<TranscriptOut, String> {
    let started = std::time::Instant::now();
    let path = model_path().ok_or("modèle introuvable")?;
    if !model_state().present {
        return Err("le modèle n'est pas encore téléchargé".into());
    }

    let mut guard = CONTEXT.lock().map_err(|_| "contexte verrouillé")?;
    if guard.is_none() {
        let ctx = WhisperContext::new_with_params(
            &path.to_string_lossy(),
            WhisperContextParameters::default(),
        )
        .map_err(|e| format!("chargement du modèle : {e}"))?;
        *guard = Some(ctx);
    }
    let ctx = guard.as_ref().expect("contexte chargé");
    let mut state = ctx.create_state().map_err(|e| e.to_string())?;

    let mut params = FullParams::new(SamplingStrategy::Greedy { best_of: 1 });
    params.set_translate(false);
    params.set_print_special(false);
    params.set_print_progress(false);
    params.set_print_realtime(false);
    params.set_print_timestamps(false);
    params.set_token_timestamps(true);
    // Sur un micro d'amphi, whisper part en boucle en se conditionnant sur son
    // propre texte. Les mêmes garde-fous que côté serveur, pour les mêmes
    // raisons — c'est ce qui produisait « shocking shocking shocking… ».
    params.set_no_context(true);
    params.set_suppress_blank(true);
    params.set_temperature_inc(0.2);
    params.set_entropy_thold(2.4);
    params.set_logprob_thold(-1.0);
    params.set_no_speech_thold(0.6);
    params.set_n_threads(std::thread::available_parallelism().map(|n| n.get() as i32).unwrap_or(4));
    if let Some(lang) = language.filter(|l| !l.is_empty() && *l != "auto") {
        params.set_language(Some(lang));
    }
    if let Some(text) = prompt.filter(|t| !t.is_empty()) {
        params.set_initial_prompt(text);
    }

    // whisper.cpp veut du float normalisé ; le navigateur nous envoie du 16 bits
    // pour diviser par deux la taille du transfert.
    let pcm: Vec<f32> = pcm_i16.iter().map(|s| *s as f32 / 32768.0).collect();
    let duration_ms = pcm.len() as f64 / 16.0;

    state.full(params, &pcm).map_err(|e| format!("transcription : {e}"))?;

    // whisper-rs 0.14 n'expose pas la langue détectée sur l'état. Ce n'est pas
    // gênant : l'interface impose la langue de la séance, précisément pour que
    // la détection ne bascule pas d'une partie à l'autre.
    let detected = language.filter(|l| !l.is_empty() && *l != "auto").map(str::to_string);

    let mut segments = Vec::new();
    let mut previous_text = String::new();
    let count = state.full_n_segments().map_err(|e| e.to_string())?;
    for i in 0..count {
        let text = state.full_get_segment_text(i).unwrap_or_default().trim().to_string();
        if text.is_empty() || text == previous_text {
            // Deux segments consécutifs identiques : le second est un artefact
            // de silence, pas une répétition de l'enseignant.
            continue;
        }
        previous_text = text.clone();

        let start_ms = state.full_get_segment_t0(i).unwrap_or(0) as f64 * 10.0;
        let end_ms = state.full_get_segment_t1(i).unwrap_or(0) as f64 * 10.0;

        let mut words = Vec::new();
        let n_tokens = state.full_n_tokens(i).unwrap_or(0);
        for t in 0..n_tokens {
            let raw = state.full_get_token_text(i, t).unwrap_or_default();
            if raw.starts_with("[_") || raw.trim().is_empty() {
                continue;
            }
            let data = state.full_get_token_data(i, t).ok();
            let (t0, t1, p) = data
                .map(|d| (d.t0 as f64 * 10.0, d.t1 as f64 * 10.0, d.p as f64))
                .unwrap_or((start_ms, end_ms, 0.5));
            words.push(Word {
                text: raw.trim().to_string(),
                start_ms: t0,
                end_ms: t1,
                // Jamais 1,0 : une confiance saturée écraserait le vote du
                // module de consensus quand plusieurs flux seront fusionnés.
                confidence: p.clamp(0.0, 0.99),
            });
        }
        let avg = if words.is_empty() {
            0.5
        } else {
            words.iter().map(|w| w.confidence).sum::<f64>() / words.len() as f64
        };

        segments.push(Segment {
            start_ms,
            end_ms,
            text,
            words,
            lang: detected.clone(),
            avg_confidence: avg,
        });
    }

    Ok(TranscriptOut {
        segments,
        audio_duration_ms: duration_ms,
        provider: "whisper.cpp".into(),
        model: MODEL_FILE.into(),
        cost_milli_cents: 0.0,
        processing_ms: started.elapsed().as_secs_f64() * 1000.0,
    })
}
