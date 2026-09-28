#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde_json::{json, Value};
use std::{io::Write, path::PathBuf, process::{Command, Stdio}};
#[cfg(windows)]
use std::os::windows::process::CommandExt;
use tauri::{Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

fn project_root() -> PathBuf {
    std::env::var_os("DSH_TAURI_ROOT").map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")))
}

fn config() -> Result<Value, String> {
    let path = project_root().join("backend.json");
    serde_json::from_slice(&std::fs::read(path).map_err(|e| format!("无法读取 backend.json：{e}"))?)
        .map_err(|e| format!("backend.json 格式错误：{e}"))
}

fn local_manager(label: &str, url: &tauri::Url) -> bool {
    label == "manager" && (url.scheme() == "tauri" && url.host_str() == Some("localhost")
        || matches!(url.scheme(), "http" | "https") && url.host_str() == Some("tauri.localhost"))
}

fn authorized(window: &WebviewWindow) -> Result<(), String> {
    let url = window.url().map_err(|e| e.to_string())?;
    if !local_manager(window.label(), &url) {
        return Err("仅本地管理页面可以执行维护操作。".into());
    }
    Ok(())
}

fn helper(request: Value) -> Result<Value, String> {
    let config = config()?;
    let root = project_root();
    #[cfg(windows)]
    let mut command = {
        let script = root.join("scripts/maintenance.py").to_string_lossy().replace('\\', "/");
        let bytes = script.as_bytes();
        if bytes.len() < 3 || bytes[1] != b':' || !bytes[0].is_ascii_alphabetic() {
            return Err("The project must be on a Windows drive".into());
        }
        let script = format!("/mnt/{}/{}", (bytes[0] as char).to_ascii_lowercase(), &script[3..]);
        let mut command = Command::new("wsl.exe");
        command.args(["-d", config["distribution"].as_str().ok_or("缺少 WSL distribution")?,
                      "-u", config["user"].as_str().ok_or("缺少 WSL user")?, "--", "python3", "-B", &script]);
        command.creation_flags(0x08000000);
        command
    };
    #[cfg(not(windows))]
    let mut command = {
        let mut command = Command::new("python3");
        command.arg("-B").arg(root.join("scripts/maintenance.py"));
        command
    };
    command.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = command.spawn().map_err(|e| format!("无法启动独立管理程序：{e}"))?;
    let payload = serde_json::to_vec(&json!({"config": config, "request": request})).map_err(|e| e.to_string())?;
    child.stdin.take().ok_or("管理程序输入不可用")?.write_all(&payload).map_err(|e| e.to_string())?;
    // wait_with_output drains both pipes. Long npm output goes to the helper's
    // local log, never to an undrained IPC pipe. Subprocess deadlines live there.
    let output = child.wait_with_output().map_err(|e| e.to_string())?;
    let value: Value = serde_json::from_slice(&output.stdout)
        .map_err(|_| format!("管理程序未返回有效结果：{}", String::from_utf8_lossy(&output.stderr)))?;
    if !output.status.success() || value.get("error").is_some() {
        return Err(value["error"].as_str().unwrap_or("管理操作失败").to_string());
    }
    Ok(value)
}

fn validated_backend(value: &Value, settings: &Value) -> Result<tauri::Url, String> {
    let safe = value["mode"] == "safe";
    let normal_port = settings["port"].as_u64().ok_or("缺少后端端口")?;
    let port = if safe { settings["safe_port"].as_u64().unwrap_or(normal_port + 1) } else { normal_port };
    let url = tauri::Url::parse(value["url"].as_str().ok_or("缺少后端地址")?).map_err(|_| "无效的后端地址")?;
    if url.scheme() != "http" || !matches!(url.host_str(), Some("127.0.0.1" | "localhost"))
        || url.port().map(u64::from) != Some(port) || url.path() != "/"
        || !url.username().is_empty() || url.password().is_some() || url.fragment().is_some()
        || !url.query_pairs().any(|(key, value)| key == "token" && !value.is_empty()) {
        return Err("拒绝非预期的本机后端地址。".into());
    }
    Ok(url)
}

#[tauri::command]
async fn manage(window: WebviewWindow, app: tauri::AppHandle, request: Value) -> Result<Value, String> {
    authorized(&window)?;
    let action = request["action"].as_str().unwrap_or("").to_string();
    let supported = ["status", "versions", "version_info", "open_normal", "open_safe", "stop_normal", "stop_safe",
                     "install_core", "switch_core", "plugin_install", "plugin_remove", "plugin_toggle"];
    if !supported.contains(&action.as_str()) { return Err("不支持的管理操作。".into()); }
    let value = tauri::async_runtime::spawn_blocking(move || helper(request)).await.map_err(|e| e.to_string())??;
    if action == "open_normal" || action == "open_safe" {
        let settings = config()?;
        let url = validated_backend(&value, &settings)?;
        let label = if action == "open_safe" { "safe" } else { "main" };
        let port = url.port();
        // New native WebView starts directly at the token URL so Strict cookies
        // survive WebKit's first navigation. Backend pages receive no local IPC.
        let title = format!("DeepSeek Harness · {} · {}",
            if label == "safe" { "安全模式" } else { "普通模式" }, value["version"].as_str().unwrap_or("未知版本"));
        if let Some(existing) = app.get_webview_window(label) {
            existing.navigate(url).map_err(|e| e.to_string())?;
            existing.set_title(&title).map_err(|e| e.to_string())?;
            existing.show().map_err(|e| e.to_string())?;
            existing.set_focus().map_err(|e| e.to_string())?;
            return Ok(json!({"message": "已打开 DeepSeek Harness。"}));
        }
        let mut builder = WebviewWindowBuilder::new(&app, label, WebviewUrl::External(url));
        if label == "safe" {
            // Cookies are not scoped by port: give safe mode its own store so
            // its login token cannot log the ordinary WebUI out.
            let directory = app.path().app_local_data_dir().map_err(|e| e.to_string())?.join("safe-webview");
            std::fs::create_dir_all(&directory).map_err(|e| e.to_string())?;
            builder = builder.data_directory(directory);
        }
        builder.title(title).inner_size(1280.0, 860.0).min_inner_size(800.0, 560.0)
            .on_navigation(move |url| url.scheme() == "http"
                && matches!(url.host_str(), Some("127.0.0.1" | "localhost")) && url.port() == port)
            .build().map_err(|e| format!("无法打开后端窗口：{e}"))?;
        // Authentication tokens never go to the management DOM or its logs.
        return Ok(json!({"message": "已打开 DeepSeek Harness。"}));
    }
    if ["install_core", "switch_core", "stop_normal", "plugin_install", "plugin_remove", "plugin_toggle"].contains(&action.as_str()) {
        if let Some(window) = app.get_webview_window("main") { let _ = window.close(); }
    }
    if ["install_core", "switch_core", "stop_safe"].contains(&action.as_str()) {
        if let Some(window) = app.get_webview_window("safe") { let _ = window.close(); }
    }
    Ok(value)
}

#[tauri::command]
fn startup_mode(window: WebviewWindow) -> Result<String, String> {
    authorized(&window)?;
    let args: Vec<String> = std::env::args().collect();
    Ok(if args.iter().any(|a| a == "--safe") { "safe" }
       else if args.iter().any(|a| a == "--normal") { "normal" } else { "manage" }.into())
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![manage, startup_mode])
        .setup(|app| {
            WebviewWindowBuilder::new(app, "manager", WebviewUrl::App("index.html".into()))
                .title("DeepSeek Harness · 启动与版本管理")
                .inner_size(1100.0, 820.0).min_inner_size(820.0, 640.0)
                .on_navigation(|url| local_manager("manager", url))
                .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("Tauri application failed");
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn backend_cannot_invoke_maintenance() {
        assert!(local_manager("manager", &"tauri://localhost/index.html".parse().unwrap()));
        assert!(!local_manager("main", &"http://127.0.0.1:3080/".parse().unwrap()));
        assert!(!local_manager("manager", &"http://127.0.0.1:3080/".parse().unwrap()));
        assert!(!local_manager("manager", &"https://tauri.localhost.evil/".parse().unwrap()));
    }
    #[test]
    fn separate_backend_ports_are_enforced() {
        let settings = json!({"port":3080});
        assert!(validated_backend(&json!({"mode":"safe","url":"http://127.0.0.1:3081/?token=x"}), &settings).is_ok());
        for url in ["http://127.0.0.1:3080/?token=x", "http://user@localhost:3081/?token=x",
                    "http://evil.test:3081/?token=x", "http://localhost:3081/", "http://localhost:3081/?token=x#bad"] {
            assert!(validated_backend(&json!({"mode":"safe","url":url}), &settings).is_err());
        }
    }
}
