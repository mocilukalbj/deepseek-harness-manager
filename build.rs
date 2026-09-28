fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(&["manage", "startup_mode"]))).expect("Tauri build failed");
}
