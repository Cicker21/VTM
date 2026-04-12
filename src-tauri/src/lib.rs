use serde::{Deserialize, Serialize};
use tauri_plugin_shell::ShellExt;
use tauri::Manager;
use cpal::traits::{DeviceTrait, HostTrait};
use oauth2::{
    basic::BasicClient,
    AuthUrl, ClientId, ClientSecret, RedirectUrl, TokenResponse, TokenUrl,
    AuthorizationCode, CsrfToken, Scope, PkceCodeChallenge, RefreshToken,
};
use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::sync::{Arc, Mutex};
use std::collections::{HashMap, HashSet};
use std::fs::OpenOptions;
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::sync::Semaphore;
use url::Url;
use tauri_plugin_global_shortcut::{Shortcut, ShortcutState, GlobalShortcutExt};
use std::str::FromStr;
use tauri::Emitter;

#[cfg(windows)]
use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
    SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE,
};

include!(concat!(env!("OUT_DIR"), "/notification_sound.rs"));

fn session_log_path(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
    app.path()
        .resolve("log.txt", tauri::path::BaseDirectory::AppData)
        .map_err(|e| e.to_string())
}

fn create_or_truncate_session_log(app: &tauri::AppHandle) -> Result<(), String> {
    let path = session_log_path(app)?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }

    OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(&path)
        .map_err(|e| e.to_string())?;

    Ok(())
}

#[tauri::command]
async fn append_session_log(app: tauri::AppHandle, message: String) -> Result<(), String> {
    let path = session_log_path(&app)?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }

    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);

    let cleaned = message.replace(['\n', '\r'], " ");
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(|e| e.to_string())?;

    writeln!(file, "[{}] {}", ts, cleaned).map_err(|e| e.to_string())?;
    Ok(())
}

#[derive(Default)]
struct KeybindRegistry {
    shortcuts: Mutex<HashSet<String>>,
    actions: Mutex<HashMap<String, Vec<KeybindTrigger>>>,
    recent_presses: Mutex<HashMap<String, (u64, u8)>>,
    recent_replays: Mutex<HashMap<String, u64>>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct KeybindTrigger {
    action: String,
    #[serde(default = "default_press_count")]
    press_count: u8,
}

#[derive(Debug, Deserialize)]
struct KeybindSpec {
    shortcut: String,
    action: String,
    #[serde(default = "default_press_count")]
    #[serde(alias = "pressCount")]
    press_count: u8,
}

fn default_press_count() -> u8 {
    1
}

fn now_millis() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

const DOUBLE_PRESS_WINDOW_MS: u64 = 450;

fn emit_keybind_actions(app: &tauri::AppHandle, triggers: &[KeybindTrigger], press_count: u8) {
    for trigger in triggers {
        if trigger.press_count == press_count {
            let _ = app.emit("keybind-action", trigger.action.clone());
        }
    }
}

fn normalize_shortcut_key(shortcut: &str) -> String {
    let lower = shortcut.trim().to_lowercase();
    match lower.as_str() {
        "mediaplaypause" | "mediaplay" | "mediapause" | "mediastop" => "mediaplaypause".to_string(),
        "audiovolumeup" | "volumeup" | "volumeincrease" => "volumeup".to_string(),
        "audiovolumedown" | "volumedown" | "volumedecrease" => "volumedown".to_string(),
        "numpadadd" | "add" | "plus" => "numpadadd".to_string(),
        "numpadsubtract" | "subtract" | "minus" => "numpadsubtract".to_string(),
        "numpadmultiply" | "multiply" | "asterisk" => "numpadmultiply".to_string(),
        "numpaddivide" | "divide" => "numpaddivide".to_string(),
        _ => lower,
    }
}

fn shortcut_candidates(shortcut: &str) -> Vec<String> {
    let trimmed = shortcut.trim();
    let lower = trimmed.to_lowercase();

    match lower.as_str() {
        "mediaplaypause" | "mediaplay" | "mediapause" | "mediastop" => vec![
            "MediaPlayPause".to_string(),
            "MediaPlay".to_string(),
            "MediaPause".to_string(),
            "MediaStop".to_string(),
        ],
        "volumeup" | "audiovolumeup" | "volumeincrease" => vec![
            "AudioVolumeUp".to_string(),
            "VolumeUp".to_string(),
            "VolumeIncrease".to_string(),
        ],
        "volumedown" | "audiovolumedown" | "volumedecrease" => vec![
            "AudioVolumeDown".to_string(),
            "VolumeDown".to_string(),
            "VolumeDecrease".to_string(),
        ],
        "numpadmultiply" | "multiply" | "asterisk" => vec![
            "NumpadMultiply".to_string(),
        ],
        "numpaddivide" | "divide" => vec![
            "NumpadDivide".to_string(),
        ],
        _ => vec![trimmed.to_string()],
    }
}

fn is_pass_through_shortcut(shortcut: &str) -> bool {
    let normalized = normalize_shortcut_key(shortcut);
    matches!(normalized.as_str(), "numpadadd" | "numpadsubtract" | "numpadmultiply" | "numpaddivide")
}

fn shortcut_virtual_key(shortcut: &str) -> Option<u16> {
    let normalized = normalize_shortcut_key(shortcut);

    match normalized.as_str() {
        "numpadadd" => Some(0x6B),
        "numpadsubtract" => Some(0x6D),
        "numpadmultiply" => Some(0x6A),
        "numpaddivide" => Some(0x6F),
        "mediaplaypause" => Some(0xB3),
        "volumeup" => Some(0xAF),
        "volumedown" => Some(0xAE),
        _ => normalized
            .strip_prefix('f')
            .and_then(|value| value.parse::<u16>().ok())
            .filter(|value| (1..=24).contains(value))
            .map(|value| 0x70 + (value - 1)),
    }
}

fn shortcut_text_char(shortcut: &str) -> Option<char> {
    match normalize_shortcut_key(shortcut).as_str() {
        "numpadadd" => Some('+'),
        "numpadsubtract" => Some('-'),
        "numpadmultiply" => Some('*'),
        "numpaddivide" => Some('/'),
        _ => None,
    }
}

#[cfg(windows)]
fn replay_shortcut(shortcut: &str) -> Result<(), String> {
    if let Some(ch) = shortcut_text_char(shortcut) {
        let scan = ch as u16;
        let inputs = [
            INPUT {
                r#type: INPUT_KEYBOARD,
                Anonymous: INPUT_0 {
                    ki: KEYBDINPUT {
                        wVk: 0,
                        wScan: scan,
                        dwFlags: KEYEVENTF_UNICODE,
                        time: 0,
                        dwExtraInfo: 0,
                    },
                },
            },
            INPUT {
                r#type: INPUT_KEYBOARD,
                Anonymous: INPUT_0 {
                    ki: KEYBDINPUT {
                        wVk: 0,
                        wScan: scan,
                        dwFlags: KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                        time: 0,
                        dwExtraInfo: 0,
                    },
                },
            },
        ];

        let sent = unsafe {
            SendInput(
                inputs.len() as u32,
                inputs.as_ptr(),
                std::mem::size_of::<INPUT>() as i32,
            )
        };

        if sent == 0 {
            return Err(std::io::Error::last_os_error().to_string());
        }

        return Ok(());
    }

    let Some(vk) = shortcut_virtual_key(shortcut) else {
        return Ok(());
    };

    let inputs = [
        INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT {
                    wVk: vk,
                    wScan: 0,
                    dwFlags: 0,
                    time: 0,
                    dwExtraInfo: 0,
                },
            },
        },
        INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT {
                    wVk: vk,
                    wScan: 0,
                    dwFlags: KEYEVENTF_KEYUP,
                    time: 0,
                    dwExtraInfo: 0,
                },
            },
        },
    ];

    let sent = unsafe {
        SendInput(
            inputs.len() as u32,
            inputs.as_ptr(),
            std::mem::size_of::<INPUT>() as i32,
        )
    };

    if sent == 0 {
        return Err(std::io::Error::last_os_error().to_string());
    }

    Ok(())
}

#[cfg(not(windows))]
fn replay_shortcut(shortcut: &str) -> Result<(), String> {
    let _ = shortcut;
    Ok(())
}

#[cfg(windows)]
fn replay_shortcut_passthrough(
    _app: &tauri::AppHandle,
    registry: &KeybindRegistry,
    shortcut_text: &str,
    shortcut_key: &str,
) {
    mark_recent_replay(registry, shortcut_key);
    let _ = replay_shortcut(shortcut_text);
}

#[cfg(not(windows))]
fn replay_shortcut_passthrough(
    _app: &tauri::AppHandle,
    registry: &KeybindRegistry,
    shortcut_text: &str,
    shortcut_key: &str,
) {
    mark_recent_replay(registry, shortcut_key);
    let _ = replay_shortcut(shortcut_text);
}

fn mark_recent_replay(registry: &KeybindRegistry, shortcut_key: &str) {
    if let Ok(mut replays) = registry.recent_replays.lock() {
        replays.insert(shortcut_key.to_string(), now_millis());
    }
}

fn should_skip_recent_replay(registry: &KeybindRegistry, shortcut_key: &str) -> bool {
    let now = now_millis();
    let mut replays = match registry.recent_replays.lock() {
        Ok(guard) => guard,
        Err(_) => return false,
    };

    let stale_keys: Vec<String> = replays
        .iter()
        .filter_map(|(key, ts)| {
            if now.saturating_sub(*ts) > 120 {
                Some(key.clone())
            } else {
                None
            }
        })
        .collect();
    for key in stale_keys {
        replays.remove(&key);
    }

    if let Some(ts) = replays.get(shortcut_key) {
        if now.saturating_sub(*ts) <= 120 {
            replays.remove(shortcut_key);
            return true;
        }
    }

    false
}

fn is_safe_global_shortcut(shortcut: &str) -> bool {
    let trimmed = shortcut.trim();
    if trimmed.is_empty() {
        return false;
    }

    if trimmed.contains('+') {
        return true;
    }

    let normalized = normalize_shortcut_key(trimmed);
    if matches!(
        normalized.as_str(),
        "mediaplaypause"
            | "volumeup"
            | "volumedown"
            | "numpadadd"
            | "numpadsubtract"
            | "numpadmultiply"
            | "numpaddivide"
    ) {
        return true;
    }

    if let Some(function_key) = normalized.strip_prefix('f').and_then(|value| value.parse::<u8>().ok()) {
        return (1..=24).contains(&function_key);
    }

    false
}

#[tauri::command]
async fn sync_keybinds(
    app: tauri::AppHandle,
    registry: tauri::State<'_, KeybindRegistry>,
    keybinds: Vec<KeybindSpec>,
) -> Result<(), String> {
    let previous_shortcuts = {
        let shortcuts = registry.shortcuts.lock().map_err(|e| e.to_string())?;
        shortcuts.iter().cloned().collect::<Vec<_>>()
    };
    let previous_actions = {
        let actions = registry.actions.lock().map_err(|e| e.to_string())?;
        actions.clone()
    };
    let previous_recent_presses = {
        let recent_presses = registry.recent_presses.lock().map_err(|e| e.to_string())?;
        recent_presses.clone()
    };

    let mut parsed_bindings: Vec<(String, String, String, u8)> = Vec::new();
    let mut shortcut_specs: HashMap<String, Shortcut> = HashMap::new();
    for binding in keybinds {
        let shortcut_text = binding.shortcut.trim().to_string();
        let action_text = binding.action.trim().to_string();
        let press_count = binding.press_count.clamp(1, 2);
        if shortcut_text.is_empty() || action_text.is_empty() {
            continue;
        }

        if !is_safe_global_shortcut(&shortcut_text) {
            return Err(format!(
                "Shortcut '{}' is too broad for a global bind. Use a modifier combo like Ctrl+S or a media key.",
                shortcut_text
            ));
        }

        let mut resolved_shortcut = None;
        for candidate in shortcut_candidates(&shortcut_text) {
            if let Ok(shortcut) = Shortcut::from_str(&candidate) {
                resolved_shortcut = Some((candidate, shortcut));
                break;
            }
        }

        let Some((registered_shortcut_text, shortcut)) = resolved_shortcut else {
            return Err(format!("Could not parse shortcut '{}'", shortcut_text));
        };

        let shortcut_key = normalize_shortcut_key(&registered_shortcut_text);
        shortcut_specs.entry(shortcut_key.clone()).or_insert(shortcut);
        parsed_bindings.push((registered_shortcut_text.clone(), action_text, shortcut_key, press_count));
    }

    for shortcut_text in previous_shortcuts.iter() {
        if let Ok(shortcut) = Shortcut::from_str(shortcut_text) {
            let _ = app.global_shortcut().unregister(shortcut);
        }
    }

    {
        let mut shortcuts = registry.shortcuts.lock().map_err(|e| e.to_string())?;
        let mut actions = registry.actions.lock().map_err(|e| e.to_string())?;
        let mut recent_presses = registry.recent_presses.lock().map_err(|e| e.to_string())?;
        shortcuts.clear();
        actions.clear();
        recent_presses.clear();

        for (shortcut_key, shortcut) in shortcut_specs.iter() {
            if let Err(err) = app.global_shortcut().register(*shortcut) {
                for text in shortcuts.iter() {
                    if let Ok(existing_shortcut) = Shortcut::from_str(text) {
                        let _ = app.global_shortcut().unregister(existing_shortcut);
                    }
                }
                for previous_shortcut in previous_shortcuts.iter() {
                    if let Ok(existing_shortcut) = Shortcut::from_str(previous_shortcut) {
                        let _ = app.global_shortcut().register(existing_shortcut);
                    }
                }
                if let Ok(mut restore_shortcuts) = registry.shortcuts.lock() {
                    *restore_shortcuts = previous_shortcuts.clone().into_iter().collect();
                }
                if let Ok(mut restore_actions) = registry.actions.lock() {
                    *restore_actions = previous_actions;
                }
                if let Ok(mut restore_recent_presses) = registry.recent_presses.lock() {
                    *restore_recent_presses = previous_recent_presses;
                }
                return Err(err.to_string());
            }

            shortcuts.insert(shortcut_key.clone());
        }

        for (shortcut_text, action_text, shortcut_key, press_count) in parsed_bindings {
            actions
                .entry(shortcut_key)
                .or_insert_with(Vec::new)
                .push(KeybindTrigger { action: action_text, press_count });
            shortcuts.insert(shortcut_text);
        }
    }

    Ok(())
}

fn is_video_id_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '-' || c == '_'
}

fn is_valid_video_id(input: &str) -> bool {
    let trimmed = input.trim();
    trimmed.len() == 11 && trimmed.chars().all(is_video_id_char)
}

fn normalize_playlist_id(input: &str) -> String {
    let trimmed = input.trim();
    if trimmed.is_empty() {
        return String::new();
    }

    if let Ok(url) = Url::parse(trimmed) {
        if let Some((_, value)) = url.query_pairs().find(|(k, _)| k == "list") {
            let id = value.into_owned();
            if !id.trim().is_empty() {
                return id;
            }
        }
    }

    if let Some(pos) = trimmed.find("list=") {
        let after = &trimmed[pos + 5..];
        let end = after.find('&').unwrap_or(after.len());
        let id = after[..end].trim();
        if !id.is_empty() {
            return id.to_string();
        }
    }

    trimmed.to_string()
}

fn extract_video_id_from_url_candidate(input: &str) -> Option<String> {
    if let Ok(url) = Url::parse(input) {
        if let Some((_, value)) = url.query_pairs().find(|(k, _)| k == "v") {
            let id = value.into_owned();
            if id.len() == 11 && id.chars().all(is_video_id_char) {
                return Some(id);
            }
        }
    }

    if let Some(pos) = input.find("v=") {
        let after = &input[pos + 2..];
        let end = after.find('&').unwrap_or(after.len());
        let id = after[..end].trim();
        if id.len() == 11 && id.chars().all(is_video_id_char) {
            return Some(id.to_string());
        }
    }

    None
}

fn compact_playlist_payload(raw: &serde_json::Value, fallback_playlist_id: &str) -> serde_json::Value {
    let playlist_id = raw
        .get("id")
        .and_then(|v| v.as_str())
        .filter(|s| !s.trim().is_empty())
        .map(|s| s.to_string())
        .unwrap_or_else(|| fallback_playlist_id.to_string());

    let playlist_title = raw
        .get("title")
        .and_then(|v| v.as_str())
        .or_else(|| raw.get("fulltitle").and_then(|v| v.as_str()))
        .filter(|s| !s.trim().is_empty())
        .map(|s| s.to_string())
        .unwrap_or_else(|| format!("Playlist {}", playlist_id));

    let entries = raw
        .get("entries")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();

    let mut compact_entries = Vec::<serde_json::Value>::new();
    let mut seen_ids = HashSet::<String>::new();

    for entry in entries {
        let id = entry
            .get("id")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .or_else(|| {
                entry
                    .get("url")
                    .and_then(|v| v.as_str())
                    .and_then(extract_video_id_from_url_candidate)
            });

        let Some(id) = id else {
            continue;
        };
        if id.len() != 11 || !id.chars().all(is_video_id_char) {
            continue;
        }
        if !seen_ids.insert(id.clone()) {
            continue;
        }

        let title = entry
            .get("title")
            .and_then(|v| v.as_str())
            .or_else(|| entry.get("fulltitle").and_then(|v| v.as_str()))
            .or_else(|| entry.get("track").and_then(|v| v.as_str()))
            .unwrap_or("Unknown")
            .to_string();

        let thumbnail = entry
            .get("thumbnail")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string())
            .or_else(|| {
                entry
                    .get("thumbnails")
                    .and_then(|v| v.as_array())
                    .and_then(|arr| arr.last())
                    .and_then(|thumb| thumb.get("url"))
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string())
            })
            .unwrap_or_else(|| format!("https://i.ytimg.com/vi/{}/mqdefault.jpg", id));

        let mut compact_entry = serde_json::json!({
            "id": id,
            "title": title,
            "thumbnail": thumbnail,
            "uploader": entry
                .get("uploader")
                .and_then(|v| v.as_str())
                .or_else(|| entry.get("channel").and_then(|v| v.as_str()))
                .or_else(|| entry.get("artist").and_then(|v| v.as_str()))
                .unwrap_or("YouTube")
        });

        if let Some(duration) = entry.get("duration").and_then(|v| v.as_f64()) {
            compact_entry["duration"] = serde_json::json!(duration);
        }
        if let Some(availability) = entry.get("availability").and_then(|v| v.as_str()) {
            compact_entry["availability"] = serde_json::json!(availability);
        }
        if let Some(available) = entry.get("available").and_then(|v| v.as_bool()) {
            compact_entry["available"] = serde_json::json!(available);
        }

        compact_entries.push(compact_entry);
    }

    serde_json::json!({
        "id": playlist_id,
        "title": playlist_title,
        "entries": compact_entries,
        "source": "ytdlp"
    })
}

fn has_meaningful_data(filename: &str, data: &serde_json::Value) -> bool {
    match filename {
        "settings.json" => data.as_object().map(|o| !o.is_empty()).unwrap_or(false),
        "playlists.json" => {
            if let Some(playlists) = data.get("playlists").and_then(|v| v.as_object()) {
                return !playlists.is_empty();
            }
            data.as_object().map(|o| !o.is_empty()).unwrap_or(false)
        }
        "favorites.json" => {
            if let Some(arr) = data.get("favorites").and_then(|v| v.as_array()) {
                return !arr.is_empty();
            }
            if let Some(arr) = data.get("songs").and_then(|v| v.as_array()) {
                return !arr.is_empty();
            }
            false
        }
        "history.json" => data
            .get("history")
            .and_then(|v| v.as_array())
            .map(|a| !a.is_empty())
            .unwrap_or(false),
        "auth.json" => data.get("access_token").and_then(|v| v.as_str()).is_some(),
        _ => {
            if let Some(o) = data.as_object() {
                !o.is_empty()
            } else if let Some(a) = data.as_array() {
                !a.is_empty()
            } else {
                !data.is_null()
            }
        }
    }
}

fn is_protected_data_file(filename: &str) -> bool {
    matches!(
        filename,
        "settings.json" | "playlists.json" | "favorites.json" | "history.json"
    )
}

fn sibling_path_with_suffix(path: &std::path::Path, suffix: &str) -> std::path::PathBuf {
    let file_name = path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("data.json");
    path.with_file_name(format!("{}{}", file_name, suffix))
}

fn read_json_file(path: &std::path::Path) -> Option<serde_json::Value> {
    let file = std::fs::File::open(path).ok()?;
    serde_json::from_reader(file).ok()
}

fn extract_video_ids_from_html(html: &str) -> Vec<String> {
    let mut ids = Vec::<String>::new();
    let mut seen = HashSet::<String>::new();

    // Pattern 1: watch?v=VIDEOID
    let needle = "watch?v=";
    let mut start = 0usize;
    while let Some(pos) = html[start..].find(needle) {
        let id_start = start + pos + needle.len();
        if id_start + 11 <= html.len() {
            let candidate = &html[id_start..id_start + 11];
            if candidate.chars().all(is_video_id_char) {
                let id = candidate.to_string();
                if seen.insert(id.clone()) {
                    ids.push(id);
                }
            }
        }
        start = id_start;
        if ids.len() >= 1000 {
            break;
        }
    }

    // Pattern 1b: /watch?v=VIDEOID
    let needle1b = "/watch?v=";
    let mut start1b = 0usize;
    while let Some(pos) = html[start1b..].find(needle1b) {
        let id_start = start1b + pos + needle1b.len();
        if id_start + 11 <= html.len() {
            let candidate = &html[id_start..id_start + 11];
            if candidate.chars().all(is_video_id_char) {
                let id = candidate.to_string();
                if seen.insert(id.clone()) {
                    ids.push(id);
                }
            }
        }
        start1b = id_start;
        if ids.len() >= 1000 {
            break;
        }
    }

    // Pattern 2: "videoId":"VIDEOID"
    let needle2 = "\"videoId\":\"";
    let mut start2 = 0usize;
    while let Some(pos) = html[start2..].find(needle2) {
        let id_start = start2 + pos + needle2.len();
        if id_start + 11 <= html.len() {
            let candidate = &html[id_start..id_start + 11];
            if candidate.chars().all(is_video_id_char) {
                let id = candidate.to_string();
                if seen.insert(id.clone()) {
                    ids.push(id);
                }
            }
        }
        start2 = id_start;
        if ids.len() >= 1000 {
            break;
        }
    }

    // Pattern 3: /video/VIDEOID (Filmot links)
    let needle3 = "/video/";
    let mut start3 = 0usize;
    while let Some(pos) = html[start3..].find(needle3) {
        let id_start = start3 + pos + needle3.len();
        if id_start + 11 <= html.len() {
            let candidate = &html[id_start..id_start + 11];
            if candidate.chars().all(is_video_id_char) {
                let id = candidate.to_string();
                if seen.insert(id.clone()) {
                    ids.push(id);
                }
            }
        }
        start3 = id_start;
        if ids.len() >= 1000 {
            break;
        }
    }

    ids
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct VideoResult {
    pub id: String,
    pub title: String,
    pub duration: Option<f64>,
    pub thumbnail: Option<String>,
    pub uploader: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct AppVersion {
    pub version: String,
    pub build: String,
    pub full_date: String,
    pub is_dev: bool,
}

pub struct SidecarManager {
    pub semaphore: Arc<Semaphore>,
}

impl SidecarManager {
    pub fn new(max_concurrent: usize) -> Self {
        Self {
            semaphore: Arc::new(Semaphore::new(max_concurrent)),
        }
    }
}

#[tauri::command]
async fn search_youtube(app: tauri::AppHandle, manager: tauri::State<'_, SidecarManager>, query: String) -> Result<Vec<VideoResult>, String> {
    let _permit = manager.semaphore.acquire().await.map_err(|e| e.to_string())?;
    let normalized_query = query.trim();
    if normalized_query.is_empty() {
        return Ok(Vec::new());
    }

    let search_query = format!("ytsearch30:{}", normalized_query);
    let output = app
        .shell()
        .sidecar("yt-dlp")
        .map_err(|e| e.to_string())?
        .args([
            &search_query,
            "--dump-json",
            "--flat-playlist",
            "--no-warnings",
        ])
        .output()
        .await
        .map_err(|e| e.to_string())?;

    if !output.status.success() {
        let err = String::from_utf8_lossy(&output.stderr);
        return Err(format!("yt-dlp error: {}", err));
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    let mut results = Vec::new();

    for line in stdout.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        if let Ok(json) = serde_json::from_str::<serde_json::Value>(line) {
            let id = json["id"].as_str().unwrap_or("").to_string();
            if id.is_empty() {
                continue;
            }
            let thumbnail = json["thumbnail"].as_str()
                .map(|s| s.to_string())
                .or_else(|| {
                    json["thumbnails"]
                        .as_array()
                        .and_then(|arr| arr.last())
                        .and_then(|t| t["url"].as_str())
                        .map(|s| s.to_string())
                });

            results.push(VideoResult {
                id,
                title: json["title"].as_str().unwrap_or("Unknown").to_string(),
                duration: json["duration"].as_f64(),
                thumbnail,
                uploader: json["uploader"]
                    .as_str()
                    .or(json["channel"].as_str())
                    .map(|s| s.to_string()),
            });
        }
    }

    Ok(results)
}

#[tauri::command]
async fn get_auto_mix_candidates(
    app: tauri::AppHandle,
    manager: tauri::State<'_, SidecarManager>,
    video_id: String,
    limit: usize,
) -> Result<Vec<VideoResult>, String> {
    let _permit = manager.semaphore.acquire().await.map_err(|e| e.to_string())?;

    let current_id = video_id.trim();
    if !is_valid_video_id(current_id) {
        return Err("Invalid video id for auto mix".to_string());
    }

    let capped_limit = limit.clamp(1, 100);
    let playlist_end = capped_limit.to_string();
    let mix_url = format!(
        "https://www.youtube.com/watch?v={}&list=RD{}",
        current_id, current_id
    );

    let output = app
        .shell()
        .sidecar("yt-dlp")
        .map_err(|e| e.to_string())?
        .args([
            "--dump-json",
            "--flat-playlist",
            "--no-warnings",
            "--ignore-errors",
            "--ignore-no-formats-error",
            "--playlist-end",
            &playlist_end,
            &mix_url,
        ])
        .output()
        .await
        .map_err(|e| e.to_string())?;

    let stdout = String::from_utf8_lossy(&output.stdout);
    let mut results = Vec::new();
    let mut seen_ids = HashSet::<String>::new();

    for line in stdout.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }

        if let Ok(json) = serde_json::from_str::<serde_json::Value>(line) {
            let id = json["id"].as_str().unwrap_or("").to_string();
            if !is_valid_video_id(&id) {
                continue;
            }
            if id == current_id {
                continue;
            }
            if !seen_ids.insert(id.clone()) {
                continue;
            }

            let thumbnail = json["thumbnail"]
                .as_str()
                .map(|s| s.to_string())
                .or_else(|| {
                    json["thumbnails"]
                        .as_array()
                        .and_then(|arr| arr.last())
                        .and_then(|t| t["url"].as_str())
                        .map(|s| s.to_string())
                })
                .or_else(|| Some(format!("https://i.ytimg.com/vi/{}/mqdefault.jpg", id)));

            results.push(VideoResult {
                id,
                title: json["title"].as_str().unwrap_or("Unknown").to_string(),
                duration: json["duration"].as_f64(),
                thumbnail,
                uploader: json["uploader"]
                    .as_str()
                    .or(json["channel"].as_str())
                    .map(|s| s.to_string()),
            });

            if results.len() >= capped_limit {
                break;
            }
        }
    }

    if results.is_empty() {
        let err = String::from_utf8_lossy(&output.stderr);
        return Err(format!("No mix candidates found: {}", err.trim()));
    }

    Ok(results)
}

#[tauri::command]
async fn get_stream_url(app: tauri::AppHandle, manager: tauri::State<'_, SidecarManager>, video_id: String) -> Result<String, String> {
    let _permit = manager.semaphore.acquire().await.map_err(|e| e.to_string())?;
    let url = format!("https://www.youtube.com/watch?v={}", video_id);
    let output = app
        .shell()
        .sidecar("yt-dlp")
        .map_err(|e| e.to_string())?
        .args([
            "-f",
            "bestaudio[ext=m4a]/bestaudio/best*[vcodec=none]",
            "-g",
            "--no-warnings",
            &url,
        ])
        .output()
        .await
        .map_err(|e| e.to_string())?;

    if !output.status.success() {
        let err = String::from_utf8_lossy(&output.stderr);
        return Err(format!("yt-dlp error: {}", err));
    }

    let stream_url = String::from_utf8_lossy(&output.stdout)
        .lines()
        .next()
        .unwrap_or("")
        .trim()
        .to_string();

    if stream_url.is_empty() {
        return Err("No stream URL found".to_string());
    }

    Ok(stream_url)
}

#[tauri::command]
async fn check_ytdlp(app: tauri::AppHandle, manager: tauri::State<'_, SidecarManager>) -> Result<bool, String> {
    let _permit = manager.semaphore.acquire().await.map_err(|e| e.to_string())?;
    let result = app
        .shell()
        .sidecar("yt-dlp")
        .map_err(|e| e.to_string())?
        .args(["--version"])
        .output()
        .await;

    Ok(result.map(|o| o.status.success()).unwrap_or(false))
}

#[tauri::command]
async fn fetch_playlist(app: tauri::AppHandle, manager: tauri::State<'_, SidecarManager>, url: String) -> Result<serde_json::Value, String> {
    let _permit = manager.semaphore.acquire().await.map_err(|e| e.to_string())?;
    let playlist_id = Url::parse(&url)
        .ok()
        .and_then(|u| {
            u.query_pairs()
                .find(|(k, _)| k == "list")
                .map(|(_, v)| v.into_owned())
        })
        .unwrap_or_else(|| "unknown".to_string());

    let output = app
        .shell()
        .sidecar("yt-dlp")
        .map_err(|e| e.to_string())?
        .args([
            "--dump-single-json",
            "--flat-playlist",
            "--no-warnings",
            "--ignore-errors",
            "--ignore-no-formats-error",
            &url,
        ])
        .output()
        .await
        .map_err(|e| e.to_string())?;

    // Some hidden/deleted playlists return non-zero exit code but still include useful output.
    let primary_stdout = String::from_utf8_lossy(&output.stdout).to_string();
    if !primary_stdout.trim().is_empty() {
        if let Ok(json) = serde_json::from_str::<serde_json::Value>(&primary_stdout) {
            let entries_len = json
                .get("entries")
                .and_then(|v| v.as_array())
                .map(|a| a.len())
                .unwrap_or(0);

            if entries_len > 0 || output.status.success() {
                let mut compact = compact_playlist_payload(&json, &playlist_id);
                recover_unavailable_entries(app.clone(), &mut compact).await;
                return Ok(compact);
            }
        }
    }

    // Fallback: parse flat line-by-line JSON entries even if command failed.
    let flat_output = app
        .shell()
        .sidecar("yt-dlp")
        .map_err(|e| e.to_string())?
        .args([
            "--dump-json",
            "--flat-playlist",
            "--no-warnings",
            "--ignore-errors",
            "--ignore-no-formats-error",
            &url,
        ])
        .output()
        .await
        .map_err(|e| e.to_string())?;

    let flat_stdout = String::from_utf8_lossy(&flat_output.stdout);
    let mut entries = Vec::<serde_json::Value>::new();
    let mut seen_ids = HashSet::<String>::new();

    for line in flat_stdout.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }

        if let Ok(json) = serde_json::from_str::<serde_json::Value>(line) {
            let id = json
                .get("id")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            if id.is_empty() {
                continue;
            }
            if !seen_ids.insert(id.clone()) {
                continue;
            }

            let thumbnail = json
                .get("thumbnail")
                .and_then(|v| v.as_str())
                .map(|s| s.to_string())
                .or_else(|| {
                    json.get("thumbnails")
                        .and_then(|v| v.as_array())
                        .and_then(|arr| arr.last())
                        .and_then(|t| t.get("url"))
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string())
                });

            let mut entry = serde_json::json!({
                "id": id,
                "title": json.get("title").and_then(|v| v.as_str()).unwrap_or("Unknown"),
            });

            if let Some(duration) = json.get("duration").and_then(|v| v.as_f64()) {
                entry["duration"] = serde_json::json!(duration);
            }
            if let Some(thumbnail) = thumbnail {
                entry["thumbnail"] = serde_json::json!(thumbnail);
            }
            if let Some(uploader) = json
                .get("uploader")
                .and_then(|v| v.as_str())
                .or_else(|| json.get("channel").and_then(|v| v.as_str()))
            {
                entry["uploader"] = serde_json::json!(uploader);
            }

            entries.push(entry);
        }
    }

    if !entries.is_empty() {
        let mut playlist_json = serde_json::json!({
            "id": playlist_id,
            "title": format!("Playlist {}", playlist_id),
            "entries": entries,
            "source": "ytdlp"
        });
        recover_unavailable_entries(app.clone(), &mut playlist_json).await;
        return Ok(playlist_json);
    }

    let err_primary = String::from_utf8_lossy(&output.stderr);
    let err_flat = String::from_utf8_lossy(&flat_output.stderr);

    // Keep import flow usable even when the playlist no longer exposes entries.
    if playlist_id != "unknown" {
        return Ok(serde_json::json!({
            "id": playlist_id,
            "title": format!("Playlist {}", playlist_id),
            "entries": [],
            "source": "unresolved",
            "error": format!("yt-dlp error: {} {}", err_primary.trim(), err_flat.trim())
        }));
    }

    Err(format!("yt-dlp error: {} {}", err_primary.trim(), err_flat.trim()))
}

#[tauri::command]
async fn save_data(app: tauri::AppHandle, filename: String, data: serde_json::Value) -> Result<(), String> {
    let app_data_path = app.path().resolve(&filename, tauri::path::BaseDirectory::AppData).map_err(|e| e.to_string())?;

    let path = app_data_path;

    // Ensure parent directory exists
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }

    // Guard against destructive empty writes for critical files when there is meaningful existing data.
    if is_protected_data_file(&filename) && !has_meaningful_data(&filename, &data) && path.exists() {
        if let Some(existing) = read_json_file(&path) {
            if has_meaningful_data(&filename, &existing) {
                return Err(format!(
                    "Refusing to overwrite '{}' with empty/non-meaningful data while existing file has meaningful content",
                    filename
                ));
            }
        }
    }

    let serialized = serde_json::to_vec_pretty(&data).map_err(|e| e.to_string())?;
    let tmp_path = sibling_path_with_suffix(&path, ".tmp");
    let bak_path = sibling_path_with_suffix(&path, ".bak");

    // Write to a temp file first.
    std::fs::write(&tmp_path, serialized).map_err(|e| e.to_string())?;

    // Always keep latest backup when current file exists.
    if path.exists() {
        std::fs::copy(&path, &bak_path).map_err(|e| e.to_string())?;
    }

    // Replace target with temp (Windows-safe fallback path).
    if let Err(rename_err) = std::fs::rename(&tmp_path, &path) {
        if path.exists() {
            std::fs::remove_file(&path).map_err(|e| e.to_string())?;
            std::fs::rename(&tmp_path, &path).map_err(|e| e.to_string())?;
        } else {
            return Err(rename_err.to_string());
        }
    }

    Ok(())
}

#[tauri::command]
async fn load_data(app: tauri::AppHandle, filename: String) -> Result<serde_json::Value, String> {
    // Always use AppData path only
    let app_data_path = app.path().resolve(&filename, tauri::path::BaseDirectory::AppData).map_err(|e| e.to_string())?;
    let backup_path = sibling_path_with_suffix(&app_data_path, ".bak");

    // If AppData file exists and has data, use it; otherwise return empty json
    if app_data_path.exists() {
        if let Some(data) = read_json_file(&app_data_path) {
            if has_meaningful_data(&filename, &data) {
                return Ok(data);
            }
        }
    }

    // Fallback to backup when main file is empty/corrupt/non-meaningful.
    if backup_path.exists() {
        if let Some(data) = read_json_file(&backup_path) {
            if has_meaningful_data(&filename, &data) {
                return Ok(data);
            }
        }
    }

    Ok(serde_json::json!({}))
}
#[derive(Debug, Serialize, Deserialize)]
pub struct VideoCheckResult {
    pub available: bool,
    pub id: Option<String>,
    pub title: Option<String>,
}

#[tauri::command]
async fn check_video_availability(app: tauri::AppHandle, manager: tauri::State<'_, SidecarManager>, video_id: String) -> Result<VideoCheckResult, String> {
    let _permit = manager.semaphore.acquire().await.map_err(|e| e.to_string())?;
    let url = format!("https://www.youtube.com/watch?v={}", video_id);
    let output = app
        .shell()
        .sidecar("yt-dlp")
        .map_err(|e| e.to_string())?
        .args([
            "--simulate",
            "--no-warnings",
            "--print", "%(id)s",
            "--print", "%(title)s",
            &url,
        ])
        .output()
        .await
        .map_err(|e| e.to_string())?;

    if !output.status.success() {
        return Ok(VideoCheckResult {
            available: false,
            id: None,
            title: None,
        });
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    let mut lines = stdout.lines();
    let id = lines.next().map(|s| s.to_string());
    let title = lines.next().map(|s| s.to_string());

    Ok(VideoCheckResult {
        available: true,
        id,
        title,
    })
}


fn is_valid_title(title: &str) -> bool {
    let lower_title = title.to_lowercase();
    let invalid_markers = [
        "deleted video", "private video",
        "just a moment", "attention required", "cloudflare",
        "enable cookies", "checking your browser", "video not found",
        "video unavailable", "sign in to confirm your age",
        "antes de ir a youtube"
    ];
    !title.is_empty() && !invalid_markers.iter().any(|m| lower_title.contains(m))
}

fn is_placeholder_title(title: &str) -> bool {
    let trimmed = title.trim();
    if trimmed.len() != 17 {
        return false;
    }

    let mut parts = trimmed.split_whitespace();
    let Some(prefix) = parts.next() else {
        return false;
    };
    let Some(video_id) = parts.next() else {
        return false;
    };

    if parts.next().is_some() {
        return false;
    }

    prefix.eq_ignore_ascii_case("video")
        && video_id.len() == 11
        && video_id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

fn entry_needs_recovery(entry: &serde_json::Value) -> bool {
    let availability = entry
        .get("availability")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_lowercase();
    let explicit_unavailable = entry
        .get("available")
        .and_then(|v| v.as_bool())
        .map(|v| !v)
        .unwrap_or(false);
    let title = entry
        .get("title")
        .and_then(|v| v.as_str())
        .unwrap_or("");

    explicit_unavailable
        || availability.contains("unavailable")
        || availability.contains("private")
        || !is_valid_title(title)
        || is_placeholder_title(title)
}

async fn recover_unavailable_entries(app: tauri::AppHandle, playlist_json: &mut serde_json::Value) {
    let Some(entries) = playlist_json.get_mut("entries").and_then(|v| v.as_array_mut()) else {
        return;
    };

    for entry in entries.iter_mut() {
        if !entry_needs_recovery(entry) {
            continue;
        }

        let Some(video_id) = entry.get("id").and_then(|v| v.as_str()).map(|s| s.to_string()) else {
            continue;
        };

        let recovered = recover_from_wayback(app.clone(), video_id.clone())
            .await
            .ok()
            .map(|title| (title, "wayback"));

        if let Some((title, source)) = recovered {
            entry["title"] = serde_json::json!(title);
            entry["available"] = serde_json::json!(false);
            entry["recovered"] = serde_json::json!(true);
            entry["recoverySource"] = serde_json::json!(source);
        }
    }
}

#[tauri::command]
async fn recover_from_techrobo(_app: tauri::AppHandle, video_id: String) -> Result<String, String> {
    let client = reqwest::Client::builder()
        .user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
        .build()
        .map_err(|e| e.to_string())?;

    let url = format!("https://findyoutubevideo.thetechrobo.ca/api/v5/{}?includeRaw=true", video_id);
    
    let resp = client.get(&url).send().await.map_err(|e| e.to_string())?;
    
    if !resp.status().is_success() {
        return Err(format!("TechRobo error: {}", resp.status()));
    }

    let json: serde_json::Value = resp.json().await.map_err(|e| e.to_string())?;
    
    // TechRobo returns a "keys" array with service objects
    if let Some(keys) = json["keys"].as_array() {
        for service in keys {
            // Check Filmot raw data first
            if service["name"] == "Filmot" {
                if let Some(raw_list) = service["rawraw"].as_array() {
                    if let Some(first) = raw_list.first() {
                        if let Some(title) = first["title"].as_str() {
                            if is_valid_title(title) {
                                return Ok(title.to_string());
                            }
                        }
                    }
                }
            }
            // Check other potential sources in rawraw if Filmot didn't have it
            if let Some(raw) = service["rawraw"].as_object() {
                if let Some(title) = raw.get("title").and_then(|v| v.as_str()) {
                    if is_valid_title(title) {
                        return Ok(title.to_string());
                    }
                }
            }
        }
    }

    Err("No se encontró el título en la base de datos de TechRobo".to_string())
}

#[tauri::command]
async fn recover_from_filmot(_app: tauri::AppHandle, video_id: String) -> Result<String, String> {
    // Keeping this as a secondary legacy option or for direct check if techrobo fails
    let client = reqwest::Client::builder()
        .user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
        .build()
        .map_err(|e| e.to_string())?;

    let filmot_url = format!("https://filmot.com/video/{}", video_id);
    
    // Add realistic headers to bypass anti-bot
    let resp_result = client.get(&filmot_url)
        .header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8")
        .header("Accept-Language", "en-US,en;q=0.9,es;q=0.8")
        .header("Sec-Ch-Ua", "\"Chromium\";v=\"122\", \"Not(A:Brand\";v=\"24\", \"Google Chrome\";v=\"122\"")
        .header("Sec-Ch-Ua-Mobile", "?0")
        .header("Sec-Ch-Ua-Platform", "\"Windows\"")
        .header("Sec-Fetch-Dest", "document")
        .header("Sec-Fetch-Mode", "navigate")
        .header("Sec-Fetch-Site", "none")
        .header("Sec-Fetch-User", "?1")
        .header("Upgrade-Insecure-Requests", "1")
        .send()
        .await;

    if let Ok(resp) = resp_result {
        let status = resp.status();
        if status.is_success() {
            if let Ok(html) = resp.text().await {
                let html_lower = html.to_lowercase();
                
                // Primary: Try <h5> tag which often contains the name on Filmot
                if let Some(h5_idx) = html_lower.find("<h5") {
                    let after_h5 = &html[h5_idx..];
                    if let Some(tag_end) = after_h5.find(">") {
                        let content_start = h5_idx + tag_end + 1;
                        if let Some(end_tag) = html_lower[content_start..].find("</h5>") {
                            let title = html[content_start..content_start + end_tag]
                                .replace("&quot;", "\"")
                                .replace("&amp;", "&")
                                .replace("&#39;", "'")
                                .trim()
                                .to_string();
                            if is_valid_title(&title) { return Ok(title); }
                        }
                    }
                }

                // Secondary: Try <title> tag
                if let Some(start_tag_idx) = html_lower.find("<title") {
                    let after_tag = &html[start_tag_idx..];
                    if let Some(tag_end_idx) = after_tag.find(">") {
                        let content_start = start_tag_idx + tag_end_idx + 1;
                        if let Some(end_tag_idx) = html_lower[content_start..].find("</title>") {
                            let raw_title = &html[content_start..content_start + end_tag_idx];
                            let title = raw_title
                                .replace("&quot;", "\"")
                                .replace("&amp;", "&")
                                .replace("&#39;", "'")
                                .replace("&lt;", "<")
                                .replace("&gt;", ">")
                                .replace(" - Filmot", "")
                                .replace("- Filmot", "")
                                .replace("Filmot", "")
                                .trim()
                                .to_string();

                            if is_valid_title(&title) { return Ok(title); }
                        }
                    }
                }
            }
            return Err(format!("Filmot respondió exitosamente pero no se encontró un título válido (Status: {})", status));
        } else {
            return Err(format!("Filmot devolvió un error de servidor (Status: {})", status));
        }
    }
    Err("Error de red al conectar con Filmot".to_string())
}

#[tauri::command]
async fn recover_from_wayback(_app: tauri::AppHandle, video_id: String) -> Result<String, String> {
    let client = reqwest::Client::builder()
        .user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        .build()
        .map_err(|e| e.to_string())?;

    let timestamps = ["20060101", "20120101", "20180101"];
    for ts in timestamps {
        let wayback_url = format!(
            "https://archive.org/wayback/available?url=https://www.youtube.com/watch?v={}&timestamp={}",
            video_id, ts
        );
        
        if let Ok(resp) = client.get(&wayback_url).send().await {
            if resp.status().is_success() {
                if let Ok(json) = resp.json::<serde_json::Value>().await {
                    if let Some(snapshot) = json["archived_snapshots"]["closest"]["url"].as_str() {
                        if let Ok(snap_resp) = client.get(snapshot).send().await {
                            if snap_resp.status().is_success() {
                                if let Ok(html) = snap_resp.text().await {
                                    let html_lower = html.to_lowercase();
                                    if let Some(start_tag_idx) = html_lower.find("<title") {
                                        let after_tag = &html[start_tag_idx..];
                                        if let Some(tag_end_idx) = after_tag.find(">") {
                                            let content_start = start_tag_idx + tag_end_idx + 1;
                                            if let Some(end_tag_idx) = html_lower[content_start..].find("</title>") {
                                                let mut title = html[content_start..content_start + end_tag_idx]
                                                    .replace(" - YouTube", "")
                                                    .replace("- YouTube", "")
                                                    .replace("YouTube", "")
                                                    .trim()
                                                    .to_string();

                                                title = title.replace("&quot;", "\"")
                                                             .replace("&amp;", "&")
                                                             .replace("&#39;", "'")
                                                             .replace("&lt;", "<")
                                                             .replace("&gt;", ">");

                                                if is_valid_title(&title) {
                                                    return Ok(title);
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    Err("No se encontró el título en Wayback Machine".to_string())
}

#[tauri::command]
async fn recover_playlist_from_wayback(_app: tauri::AppHandle, playlist_id: String) -> Result<Vec<VideoResult>, String> {
    let client = reqwest::Client::builder()
        .user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
        .build()
        .map_err(|e| e.to_string())?;

    let normalized_id = normalize_playlist_id(&playlist_id);
    if normalized_id.is_empty() {
        return Err("wayback_playlist_id_empty".to_string());
    }

    let playlist_url = format!("https://www.youtube.com/playlist?list={}", normalized_id);
    let cdx_url = format!(
        "https://web.archive.org/cdx/search/cdx?url={}&output=json&fl=timestamp,original,statuscode,mimetype&filter=statuscode:200&filter=mimetype:text/html&limit=40",
        urlencoding::encode(&playlist_url)
    );

    let cdx_resp = client.get(&cdx_url).send().await.map_err(|e| e.to_string())?;
    if !cdx_resp.status().is_success() {
        return Err(format!("wayback_cdx_http_{}", cdx_resp.status().as_u16()));
    }

    let cdx_json: serde_json::Value = cdx_resp.json().await.map_err(|e| e.to_string())?;
    let Some(rows) = cdx_json.as_array() else {
        return Err("wayback_cdx_parse_error".to_string());
    };
    if rows.len() <= 1 {
        return Err("wayback_no_snapshots".to_string());
    }

    let mut snapshot_urls = Vec::<String>::new();
    let mut seen_snapshots = HashSet::<String>::new();

    for row in rows.iter().skip(1).rev() {
        let Some(cols) = row.as_array() else {
            continue;
        };
        let timestamp = cols.get(0).and_then(|v| v.as_str()).unwrap_or("");
        let original = cols.get(1).and_then(|v| v.as_str()).unwrap_or("");
        if timestamp.is_empty() || original.is_empty() {
            continue;
        }

        let snapshot = format!("https://web.archive.org/web/{}if_/{}", timestamp, original);
        if seen_snapshots.insert(snapshot.clone()) {
            snapshot_urls.push(snapshot);
        }
        if snapshot_urls.len() >= 25 {
            break;
        }
    }

    let mut seen_ids = HashSet::<String>::new();
    let mut results = Vec::<VideoResult>::new();

    for snapshot_url in snapshot_urls {
        let snapshot_resp = match client.get(&snapshot_url).send().await {
            Ok(resp) => resp,
            Err(_) => continue,
        };
        if !snapshot_resp.status().is_success() {
            continue;
        }

        let html = match snapshot_resp.text().await {
            Ok(text) => text,
            Err(_) => continue,
        };

        for video_id in extract_video_ids_from_html(&html) {
            if seen_ids.insert(video_id.clone()) {
                results.push(VideoResult {
                    id: video_id,
                    title: String::new(),
                    duration: None,
                    thumbnail: None,
                    uploader: None,
                });
            }
            if results.len() >= 5000 {
                break;
            }
        }

        if results.len() >= 5000 {
            break;
        }
    }

    if results.is_empty() {
        return Err("wayback_no_video_ids".to_string());
    }

    Ok(results)
}

#[tauri::command]
async fn recover_playlist_entries(app: tauri::AppHandle, playlist_id: String, source_url: Option<String>) -> Result<Vec<VideoResult>, String> {
    let mut candidates = Vec::<String>::new();

    let direct_id = normalize_playlist_id(&playlist_id);
    if !direct_id.is_empty() {
        candidates.push(direct_id);
    }

    if let Some(url) = source_url {
        let from_url = normalize_playlist_id(&url);
        if !from_url.is_empty() && !candidates.iter().any(|x| x == &from_url) {
            candidates.push(from_url);
        }
    }

    if candidates.is_empty() {
        return Err("playlist_id_not_found".to_string());
    }

    let mut wayback_errors = Vec::<String>::new();
    for candidate in &candidates {
        match recover_playlist_from_wayback(app.clone(), candidate.clone()).await {
            Ok(videos) => return Ok(videos),
            Err(e) => {
                eprintln!("Wayback recovery error ({}): {}", candidate, e);
                wayback_errors.push(format!("{}:{}", candidate, e));
            }
        }
    }

    Err(format!("playlist_recovery_failed;wayback={}", wayback_errors.join("|")))
}

#[tauri::command]
async fn list_audio_devices() -> Result<serde_json::Value, String> {
    let host = cpal::default_host();
    let mut outputs = Vec::new();
    let mut inputs = Vec::new();

    if let Ok(devices) = host.output_devices() {
        for device in devices {
            if let Ok(name) = device.name() {
                outputs.push(name);
            }
        }
    }

    if let Ok(devices) = host.input_devices() {
        for device in devices {
            if let Ok(name) = device.name() {
                inputs.push(name);
            }
        }
    }

    Ok(serde_json::json!({
        "outputs": outputs,
        "inputs": inputs
    }))
}

#[tauri::command]
async fn get_suggestions(_app: tauri::AppHandle, query: String) -> Result<Vec<String>, String> {
    let url = format!("https://suggestqueries.google.com/complete/search?client=firefox&q={}", urlencoding::encode(&query));
    let resp = reqwest::get(url).await.map_err(|e| e.to_string())?;
    let data: serde_json::Value = resp.json().await.map_err(|e| e.to_string())?;
    let suggestions = data[1]
        .as_array()
        .ok_or("Invalid response")?
        .iter()
        .filter_map(|v| v.as_str().map(|s| s.to_string()))
        .collect();
    Ok(suggestions)
}

const AUTH_URL: &str = "https://accounts.google.com/o/oauth2/v2/auth";
const TOKEN_URL: &str = "https://oauth2.googleapis.com/token";

fn oauth_client_credentials() -> Result<(String, String), String> {
    let client_id = std::env::var("GOOGLE_CLIENT_ID")
        .map_err(|_| "Missing GOOGLE_CLIENT_ID environment variable".to_string())?;
    let client_secret = std::env::var("GOOGLE_CLIENT_SECRET")
        .map_err(|_| "Missing GOOGLE_CLIENT_SECRET environment variable".to_string())?;

    if client_id.trim().is_empty() || client_secret.trim().is_empty() {
        return Err("GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET cannot be empty".to_string());
    }

    Ok((client_id, client_secret))
}

#[tauri::command]
async fn start_oauth_flow(app: tauri::AppHandle) -> Result<serde_json::Value, String> {
    let (client_id, client_secret) = oauth_client_credentials()?;
    let client = BasicClient::new(
        ClientId::new(client_id),
        Some(ClientSecret::new(client_secret)),
        AuthUrl::new(AUTH_URL.to_string()).unwrap(),
        Some(TokenUrl::new(TOKEN_URL.to_string()).unwrap()),
    )
    .set_redirect_uri(RedirectUrl::new("http://localhost:18210".to_string()).unwrap());

    let (pkce_challenge, pkce_verifier) = PkceCodeChallenge::new_random_sha256();

    let (auth_url, _csrf_token) = client
        .authorize_url(CsrfToken::new_random)
        .add_scope(Scope::new("https://www.googleapis.com/auth/youtube.readonly".to_string()))
        .add_scope(Scope::new("https://www.googleapis.com/auth/youtube".to_string()))
        .add_extra_param("access_type", "offline")
        .add_extra_param("prompt", "consent")
        .set_pkce_challenge(pkce_challenge)
        .url();

    // Open browser
    let _ = webbrowser::open(auth_url.as_str());

    // Start local server to listen for callback
    let listener = TcpListener::bind("127.0.0.1:18210").map_err(|e| e.to_string())?;
    
    // Listen for one connection
    if let Ok((mut stream, _)) = listener.accept() {
        let mut reader = BufReader::new(&stream);
        let mut request_line = String::new();
        if let Err(e) = reader.read_line(&mut request_line) {
            return Err(format!("Socket read error: {}", e));
        }

        let redirect_url = request_line.split_whitespace().nth(1).unwrap_or("");
        let full_url = format!("http://localhost{}", redirect_url);
        let url = Url::parse(&full_url).map_err(|e| e.to_string())?;
        
        let code = url.query_pairs()
            .find(|(key, _)| key == "code")
            .map(|(_, value)| AuthorizationCode::new(value.into_owned()))
            .ok_or("No code found in redirect")?;

        let token_result = client
            .exchange_code(code)
            .set_pkce_verifier(pkce_verifier)
            .request_async(oauth2::reqwest::async_http_client)
            .await
            .map_err(|e| format!("Token exchange failed: {}", e))?;

        let response = "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n<html><body style=\"font-family: sans-serif; text-align: center; padding-top: 50px;\"><h1>¡Login exitoso!</h1><p>Ya puedes cerrar esta ventana y volver a VTM.</p></body></html>";
        let _ = stream.write_all(response.as_bytes());
        let _ = stream.flush();

        let token_data = serde_json::json!({
            "access_token": token_result.access_token().secret(),
            "refresh_token": token_result.refresh_token().map(|t| t.secret()),
            "expires_in": token_result.expires_in().map(|d| d.as_secs()),
        });

        // Save token
        save_data(app, "auth.json".to_string(), token_data.clone()).await?;

        return Ok(token_data);
    }

    Err("No se pudo iniciar el servidor de callback".to_string())
}

#[tauri::command]
async fn refresh_oauth_token(app: tauri::AppHandle, refresh_token: String) -> Result<serde_json::Value, String> {
    let (client_id, client_secret) = oauth_client_credentials()?;
    let client = BasicClient::new(
        ClientId::new(client_id),
        Some(ClientSecret::new(client_secret)),
        AuthUrl::new(AUTH_URL.to_string()).unwrap(),
        Some(TokenUrl::new(TOKEN_URL.to_string()).unwrap()),
    );

    let token_result = client
        .exchange_refresh_token(&RefreshToken::new(refresh_token))
        .request_async(oauth2::reqwest::async_http_client)
        .await
        .map_err(|e| format!("Refresh token failed: {}", e))?;

    let token_data = serde_json::json!({
        "access_token": token_result.access_token().secret(),
        "refresh_token": token_result.refresh_token().map(|t| t.secret()),
        "expires_in": token_result.expires_in().map(|d| d.as_secs()),
    });

    // Save token
    save_data(app, "auth.json".to_string(), token_data.clone()).await?;

    Ok(token_data)
}

#[tauri::command]
async fn get_youtube_playlists(app: tauri::AppHandle) -> Result<serde_json::Value, String> {
    let auth_data = load_data(app, "auth.json".to_string()).await?;
    let token = auth_data["access_token"].as_str().ok_or("Not logged in")?;

    let client = reqwest::Client::new();
    let resp = client
        .get("https://www.googleapis.com/youtube/v3/playlists")
        .query(&[("part", "snippet,contentDetails"), ("mine", "true"), ("maxResults", "50")])
        .bearer_auth(token)
        .send()
        .await
        .map_err(|e| e.to_string())?;

    let json: serde_json::Value = resp.json().await.map_err(|e| e.to_string())?;
    Ok(json)
}

#[tauri::command]
async fn youtube_add_to_playlist(app: tauri::AppHandle, playlist_id: String, video_id: String) -> Result<(), String> {
    let auth_data = load_data(app, "auth.json".to_string()).await?;
    let token = auth_data["access_token"].as_str().ok_or("Not logged in")?;

    let client = reqwest::Client::new();
    let body = serde_json::json!({
        "snippet": {
            "playlistId": playlist_id,
            "resourceId": {
                "kind": "youtube#video",
                "videoId": video_id
            }
        }
    });

    let resp = client
        .post("https://www.googleapis.com/youtube/v3/playlistItems")
        .query(&[("part", "snippet")])
        .bearer_auth(token)
        .json(&body)
        .send()
        .await
        .map_err(|e| e.to_string())?;

    if !resp.status().is_success() {
        let err_text = resp.text().await.unwrap_or_default();
        return Err(format!("YouTube API error: {}", err_text));
    }

    Ok(())
}

#[tauri::command]
async fn get_youtube_playlist_items(app: tauri::AppHandle, playlist_id: String) -> Result<serde_json::Value, String> {
    let auth_data = load_data(app, "auth.json".to_string()).await?;
    let token = auth_data["access_token"].as_str().ok_or("Not logged in")?;

    let client = reqwest::Client::new();
    let mut all_items: Vec<serde_json::Value> = Vec::new();
    let mut next_page_token: Option<String> = None;

    loop {
        let mut query_params = vec![
            ("part", "snippet,contentDetails"),
            ("playlistId", &playlist_id),
            ("maxResults", "50"),
        ];

        let next_token_str = next_page_token.clone().unwrap_or_default();
        if next_page_token.is_some() {
            query_params.push(("pageToken", &next_token_str));
        }

        let resp = client
            .get("https://www.googleapis.com/youtube/v3/playlistItems")
            .query(&query_params)
            .bearer_auth(token)
            .send()
            .await
            .map_err(|e| e.to_string())?;

        if !resp.status().is_success() {
            let err_text = resp.text().await.unwrap_or_default();
            return Err(format!("YouTube API error: {}", err_text));
        }

        let mut json: serde_json::Value = resp.json().await.map_err(|e| e.to_string())?;
        
        if let Some(items) = json["items"].as_array_mut() {
            all_items.append(items);
        }

        if let Some(token) = json["nextPageToken"].as_str() {
            next_page_token = Some(token.to_string());
        } else {
            break;
        }
    }

    Ok(serde_json::json!({
        "items": all_items
    }))
}

#[tauri::command]
async fn youtube_remove_from_playlist(app: tauri::AppHandle, playlist_item_id: String) -> Result<(), String> {
    let auth_data = load_data(app, "auth.json".to_string()).await?;
    let token = auth_data["access_token"].as_str().ok_or("Not logged in")?;

    let client = reqwest::Client::new();
    let resp = client
        .delete("https://www.googleapis.com/youtube/v3/playlistItems")
        .query(&[("id", &playlist_item_id)])
        .bearer_auth(token)
        .send()
        .await
        .map_err(|e| e.to_string())?;

    if !resp.status().is_success() {
        let err_text = resp.text().await.unwrap_or_default();
        return Err(format!("YouTube API error: {}", err_text));
    }

    Ok(())
}

#[tauri::command]
async fn youtube_create_playlist(app: tauri::AppHandle, title: String) -> Result<String, String> {
    let auth_data = load_data(app, "auth.json".to_string()).await?;
    let token = auth_data["access_token"].as_str().ok_or("Not logged in")?;

    let client = reqwest::Client::new();
    let body = serde_json::json!({
        "snippet": {
            "title": title,
            "description": "Created with VTM"
        },
        "status": {
            "privacyStatus": "private"
        }
    });

    let resp = client
        .post("https://www.googleapis.com/youtube/v3/playlists")
        .query(&[("part", "snippet,status")])
        .bearer_auth(token)
        .json(&body)
        .send()
        .await
        .map_err(|e| e.to_string())?;

    if !resp.status().is_success() {
        let err_text = resp.text().await.unwrap_or_default();
        return Err(format!("YouTube API error: {}", err_text));
    }

    let json: serde_json::Value = resp.json().await.map_err(|e| e.to_string())?;
    let new_id = json["id"].as_str().ok_or("No ID returned")?;
    
    Ok(new_id.to_string())
}

#[tauri::command]
fn app_minimize(window: tauri::Window) {
    let _ = window.minimize();
}

#[tauri::command]
fn app_maximize(window: tauri::Window) {
    if let Ok(is_maximized) = window.is_maximized() {
        if is_maximized {
            let _ = window.unmaximize();
        } else {
            let _ = window.maximize();
        }
    }
}

#[tauri::command]
fn app_close(window: tauri::Window, close_to_tray: Option<bool>) {
    if close_to_tray.unwrap_or(true) {
        let _ = window.hide();
    } else {
        let _ = window.close();
    }
}

#[tauri::command]
fn app_hide(window: tauri::Window) {
    let _ = window.hide();
}

#[tauri::command]
async fn get_version_info() -> Result<AppVersion, String> {
    let version = env!("CARGO_PKG_VERSION").to_string();
    let build = option_env!("BUILD_DATE").unwrap_or("0000").to_string();
    let full_date = option_env!("BUILD_FULL_DATE").unwrap_or("00/00/0000").to_string();
    let is_dev = cfg!(debug_assertions);

    Ok(AppVersion {
        version,
        build,
        full_date,
        is_dev,
    })
}

#[tauri::command]
fn get_notification_sound_data_url() -> String {
    NOTIFICATION_SOUND_DATA_URL.to_string()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(SidecarManager::new(8)) // Limit to 8 concurrent yt-dlp processes
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            let _ = app
                .get_webview_window("main")
                .map(|w| {
                    let _ = w.show();
                    let _ = w.set_focus();
                });
        }))
        .plugin(tauri_plugin_global_shortcut::Builder::new()
            .with_handler(|app, shortcut, event| {
                if event.state() == ShortcutState::Pressed {
                    let shortcut_text = shortcut.to_string();
                    let desc = normalize_shortcut_key(&shortcut_text);
                    let keybind_registry = app.state::<KeybindRegistry>();
                    if should_skip_recent_replay(&keybind_registry, &desc) {
                        return;
                    }
                    let (press_count, press_timestamp) = {
                        let now = now_millis();
                        let mut recent = match keybind_registry.recent_presses.lock() {
                            Ok(guard) => guard,
                            Err(_) => return,
                        };
                        let entry = recent.entry(desc.clone()).or_insert((0, 0));
                        if now.saturating_sub(entry.0) <= 450 {
                            entry.1 = entry.1.saturating_add(1);
                        } else {
                            entry.1 = 1;
                        }
                        entry.0 = now;
                        (entry.1, entry.0)
                    };

                    if let Some(action_list) = app
                        .state::<KeybindRegistry>()
                        .actions
                        .lock()
                        .ok()
                        .and_then(|actions| actions.get(&desc).cloned())
                    {
                        let _ = app.emit("keybind-capture", shortcut_text.clone());

                        if is_pass_through_shortcut(&shortcut_text) {
                            replay_shortcut_passthrough(&app, &keybind_registry, &shortcut_text, &desc);
                        }

                        if press_count == 1 {
                            let single_actions: Vec<KeybindTrigger> = action_list
                                .iter()
                                .filter(|trigger| trigger.press_count == 1)
                                .cloned()
                                .collect();

                            if !single_actions.is_empty() {
                                let app_handle = app.clone();
                                let desc_clone = desc.clone();
                                thread::spawn(move || {
                                    thread::sleep(std::time::Duration::from_millis(DOUBLE_PRESS_WINDOW_MS));
                                    let should_fire = {
                                        let registry = app_handle.state::<KeybindRegistry>();
                                        let recent_presses_result = registry.recent_presses.lock();
                                        let result = match recent_presses_result {
                                            Ok(recent) => recent
                                                .get(&desc_clone)
                                                .map(|(ts, count)| *ts == press_timestamp && *count == 1)
                                                .unwrap_or(false),
                                            Err(_) => false,
                                        };
                                        result
                                    };

                                    if should_fire {
                                        emit_keybind_actions(&app_handle, &single_actions, 1);
                                        if let Ok(mut recent) = app_handle.state::<KeybindRegistry>().recent_presses.lock() {
                                            recent.remove(&desc_clone);
                                        }
                                    }
                                });
                            }
                        } else if press_count == 2 {
                            let double_actions: Vec<KeybindTrigger> = action_list
                                .iter()
                                .filter(|trigger| trigger.press_count == 2)
                                .cloned()
                                .collect();

                            if !double_actions.is_empty() {
                                emit_keybind_actions(&app, &double_actions, 2);
                            }

                            if let Ok(mut recent) = app.state::<KeybindRegistry>().recent_presses.lock() {
                                recent.remove(&desc);
                            }
                        }
                    }
                }
            })
            .build())
        .setup(|app| {
            if let Err(err) = create_or_truncate_session_log(app.handle()) {
                eprintln!("failed to initialize session log: {}", err);
            }

            app.manage(KeybindRegistry::default());

            #[cfg(desktop)]
            {
                use tauri::menu::{Menu, MenuItem};
                use tauri::tray::{TrayIconBuilder, TrayIconEvent, MouseButton};

                let quit_i = MenuItem::with_id(app, "quit", "Salir de VTM", true, None::<&str>)?;
                let show_i = MenuItem::with_id(app, "show", "Mostrar ventana", true, None::<&str>)?;
                let menu = Menu::with_items(app, &[&show_i, &quit_i])?;

                let _tray = TrayIconBuilder::new()
                    .icon(app.default_window_icon().unwrap().clone())
                    .menu(&menu)
                    .on_menu_event(|app, event| {
                        match event.id.as_ref() {
                            "quit" => {
                                app.exit(0);
                            }
                            "show" => {
                                if let Some(window) = app.get_webview_window("main") {
                                    let _ = window.show();
                                    let _ = window.set_focus();
                                }
                            }
                            _ => {}
                        }
                    })
                    .on_tray_icon_event(|tray, event| {
                        if let TrayIconEvent::Click { button: MouseButton::Left, .. } = event {
                            let app = tray.app_handle();
                            if let Some(window) = app.get_webview_window("main") {
                                let _ = window.show();
                                let _ = window.set_focus();
                            }
                        }
                    })
                    .build(app)?;
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            search_youtube,
            get_auto_mix_candidates,
            sync_keybinds,
            get_stream_url,
            check_ytdlp,
            fetch_playlist,
            save_data,
            load_data,
            get_suggestions,
            get_version_info,
            get_notification_sound_data_url,
            app_minimize,
            app_maximize,
            app_close,
            app_hide,
            check_video_availability,
            recover_from_techrobo,
            recover_from_filmot,
            recover_from_wayback,
            recover_playlist_from_wayback,
            recover_playlist_entries,
            list_audio_devices,
            append_session_log,
            start_oauth_flow,
            get_youtube_playlists,
            youtube_add_to_playlist,
            get_youtube_playlist_items,
            youtube_remove_from_playlist,
            youtube_create_playlist,
            refresh_oauth_token
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
