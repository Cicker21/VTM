use base64::Engine;
use std::fs;
use std::path::PathBuf;

fn build_notification_sound_data_url() -> String {
    let sample_rate: u32 = 22_050;
    let duration_seconds = 0.18_f32;
    let frequency = 880.0_f32;
    let amplitude = 0.25_f32;
    let sample_count = (sample_rate as f32 * duration_seconds) as usize;
    let data_chunk_size = (sample_count * 2) as u32;

    let mut wav = Vec::with_capacity(44 + sample_count * 2);
    wav.extend_from_slice(b"RIFF");
    wav.extend_from_slice(&(36 + data_chunk_size).to_le_bytes());
    wav.extend_from_slice(b"WAVE");
    wav.extend_from_slice(b"fmt ");
    wav.extend_from_slice(&16_u32.to_le_bytes());
    wav.extend_from_slice(&1_u16.to_le_bytes());
    wav.extend_from_slice(&1_u16.to_le_bytes());
    wav.extend_from_slice(&sample_rate.to_le_bytes());
    let byte_rate = sample_rate * 2;
    wav.extend_from_slice(&byte_rate.to_le_bytes());
    wav.extend_from_slice(&2_u16.to_le_bytes());
    wav.extend_from_slice(&16_u16.to_le_bytes());
    wav.extend_from_slice(b"data");
    wav.extend_from_slice(&data_chunk_size.to_le_bytes());

    for sample_index in 0..sample_count {
        let time = sample_index as f32 / sample_rate as f32;
        let attack = (time / 0.015).clamp(0.0, 1.0);
        let release = ((duration_seconds - time) / 0.03).clamp(0.0, 1.0);
        let envelope = attack.min(release);
        let wave = (2.0 * std::f32::consts::PI * frequency * time).sin();
        let sample = (wave * amplitude * envelope * i16::MAX as f32) as i16;
        wav.extend_from_slice(&sample.to_le_bytes());
    }

    let encoded = base64::engine::general_purpose::STANDARD.encode(wav);
    format!("data:audio/wav;base64,{}", encoded)
}

fn main() {
    // Re-run build.rs on every npm tauri invocation via the `pretauri` timestamp file.
    println!("cargo:rerun-if-changed=.build-ts");

    let out_dir = PathBuf::from(std::env::var("OUT_DIR").unwrap());
    let generated_path = out_dir.join("notification_sound.rs");
    let generated_source = format!(
        "pub const NOTIFICATION_SOUND_DATA_URL: &str = \"{}\";\n",
        build_notification_sound_data_url()
    );

    let _ = fs::write(generated_path, generated_source);

    let date = std::process::Command::new("powershell")
        .args(&["-Command", "Get-Date -Format 'ddMM-HHmm'"])
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_else(|_| "0000".to_string());

    let full_date = std::process::Command::new("powershell")
        .args(&["-Command", "Get-Date -Format 'dd/MM/yyyy HH:mm'"])
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_else(|_| "00/00/0000 00:00".to_string());

    println!("cargo:rustc-env=BUILD_DATE={}", date);
    println!("cargo:rustc-env=BUILD_FULL_DATE={}", full_date);
    tauri_build::build()
}
