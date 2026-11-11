use serde_json::{json, Value};
use std::io::{Read, Write};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::{atomic::{AtomicBool, Ordering}, Arc};
use tauri::{Emitter, Manager, webview::PageLoadEvent};
use tauri_plugin_opener::OpenerExt;

#[derive(Default)]
struct OperationState(Arc<AtomicBool>);

struct OperationGuard(Arc<AtomicBool>);
impl Drop for OperationGuard {
    fn drop(&mut self) { self.0.store(false, Ordering::SeqCst); }
}

fn backend_path() -> Result<PathBuf, String> {
    #[cfg(debug_assertions)]
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    #[cfg(not(debug_assertions))]
    let root = std::env::current_exe()
        .map_err(|_| "无法找到本机程序目录。")?
        .parent().and_then(|p| p.parent())
        .ok_or("请从桌面快捷方式启动工具。")?.to_path_buf();
    let script = root.join("bridge.py");
    if !script.is_file() { return Err("找不到预约程序，请保留工具目录并从桌面快捷方式启动。".into()); }
    Ok(script)
}

fn call_backend(mut request: Value) -> Result<Value, String> {
    if !request.is_object() { return Err("请求格式无效。".into()); }
    request["protocol"] = json!(2);
    request["version"] = json!(env!("CARGO_PKG_VERSION"));
    let input = serde_json::to_vec(&request).map_err(|_| "请求格式无效。")?;
    if input.len() > 65536 { return Err("请求内容过大。".into()); }
    let script = backend_path()?;
    let bundled = script.parent().ok_or("程序目录无效。")?.join("runtime/python.exe");
    let python = if bundled.is_file() { bundled }
        else if cfg!(debug_assertions) { PathBuf::from("python") }
        else { return Err("内置 Python 不完整，请重新安装程序。".into()); };
    let mut command = Command::new(python);
    command.args(["-E", "-s"]).arg(&script)
        .current_dir(script.parent().ok_or("程序目录无效。")?)
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let mut child = command.spawn().map_err(|_| "无法启动本机预约引擎，请检查 Python 安装。")?;
    let write_result = child.stdin.take().ok_or("无法连接预约引擎。")?
        .write_all(&input);
    if write_result.is_err() {
        let _ = child.kill();
        let _ = child.wait();
        return Err("无法发送请求到预约引擎。".into());
    }
    let mut output = Vec::new();
    let read_result = child.stdout.take().ok_or("无法读取预约引擎响应。")?
        .take(8 * 1024 * 1024 + 1).read_to_end(&mut output);
    if read_result.is_err() || output.len() > 8 * 1024 * 1024 {
        let _ = child.kill();
        let _ = child.wait();
        return Err("预约引擎响应超过上限或读取失败，请缩小筛选范围。".into());
    }
    let status = child.wait().map_err(|_| "预约引擎未正常返回，请刷新记录核对状态。")?;
    if !status.success() { return Err("预约引擎未正常返回，请刷新记录核对状态。".into()); }
    let response: Value = serde_json::from_slice(&output)
        .map_err(|_| "预约引擎返回格式异常，请刷新记录核对状态。")?;
    if response["ok"] == true { Ok(response["data"].clone()) }
    else {
        let message = response["error"]["message"].as_str().unwrap_or("操作未完成，请刷新后重试。");
        let ids = response["error"]["context"]["reservation_ids"].as_array();
        if let Some(ids) = ids {
            let text = ids.iter().filter_map(Value::as_str).collect::<Vec<_>>().join("、");
            Err(format!("{} 相关预约：{}", message, text))
        } else { Err(message.into()) }
    }
}

#[tauri::command]
async fn scheduler(request: Value, state: tauri::State<'_, OperationState>) -> Result<Value, String> {
    let action = request["action"].as_str().ok_or("请求格式无效。")?;
    let mutating = matches!(action, "initialize" | "prepare" | "confirm" | "cancel" | "save_account" |
        "clear_account" | "save_instance" | "delete_instance" | "replace_instance_target" |
        "save_managed_projects" | "update_project" | "save_notification_settings" | "test_notification");
    if !mutating && !matches!(action, "state" | "history" | "projects" | "parameters" | "connection" | "sync_projects" | "sync_views" | "notification_settings") {
        return Err("不支持的操作。".into());
    }
    let guard = if mutating {
        if state.0.compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst).is_err() {
            return Err("正在处理上一项操作，请稍候。".into());
        }
        Some(OperationGuard(state.0.clone()))
    } else { None };
    tauri::async_runtime::spawn_blocking(move || {
        let _guard = guard;
        call_backend(request)
    }).await.map_err(|_| "操作中断，请刷新预约记录核对状态。")?
}

#[tauri::command]
async fn open_link(mut request: Value, app: tauri::AppHandle) -> Result<(), String> {
    if !request.is_object() { return Err("链接请求格式无效。".into()); }
    request["action"] = json!("link");
    let response = tauri::async_runtime::spawn_blocking(move || call_backend(request))
        .await.map_err(|_| "无法读取链接。")??;
    let url = response["url"].as_str().ok_or("链接不存在。")?;
    app.opener().open_url(url, None::<&str>).map_err(|_| "无法打开浏览器。".into())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let script = backend_path().expect("程序文件不完整，请重新安装。");
    let root = script.parent().expect("程序目录无效。");
    if root.join("maintenance.json").exists() {
        return;
    }
    // Keep the installation closed to upgrades for the entire desktop lifetime.
    let mut options = std::fs::OpenOptions::new();
    options.read(true).write(true).create(true).truncate(false);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options.share_mode(0);
    }
    let _desktop_lock = match options.open(root.join("desktop.lock")) {
        Ok(file) => file,
        Err(_) => return,
    };
    let marker = root.join("install-location.json");
    let data = if marker.exists() {
        let value: Value = serde_json::from_slice(&std::fs::read(marker).expect("安装位置无法读取。"))
            .expect("安装位置格式无效。");
        assert_eq!(value["schema_version"], 1, "安装位置版本无效。");
        let path = PathBuf::from(value["data_directory"].as_str().expect("数据位置无效。"));
        assert!(path.is_absolute(), "数据位置必须是绝对路径。");
        path
    } else { root.join("data") };
    let profile = data.parent().expect("数据位置无效。").join("webview2");
    // Before Tauri starts threads; identical for shortcut and command-line launch.
    unsafe { std::env::set_var("WEBVIEW2_USER_DATA_FOLDER", &profile); }
    let mut context = tauri::generate_context!();
    let windows = context.config().app.windows.clone();
    for window in &mut context.config_mut().app.windows {
        window.create = false;
    }
    tauri::Builder::default()
        .setup(move |app| {
            // Config dataDirectory accepts only relative paths. The builder
            // supports the absolute installer-pinned path without AppData lookup.
            for window in &windows {
                tauri::WebviewWindowBuilder::from_config(app, window)?
                    .data_directory(profile.clone()).build()?;
            }
            Ok(())
        })
        .manage(OperationState::default())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri::plugin::Builder::<tauri::Wry>::new("external-navigation")
            .on_navigation(|_, url| {
                url.scheme() == "tauri" || matches!(url.host_str(), Some("tauri.localhost")) ||
                    (cfg!(debug_assertions) && matches!(url.host_str(), Some("localhost") | Some("127.0.0.1")))
            }).build())
        .invoke_handler(tauri::generate_handler![scheduler, open_link])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if window.state::<OperationState>().0.load(Ordering::SeqCst) {
                    api.prevent_close();
                    let _ = window.emit("operation-in-progress", ());
                }
            }
        })
        .on_page_load(|webview, payload| {
            if webview.label() == "main" && matches!(payload.event(), PageLoadEvent::Finished) {
                let _ = webview.window().show();
            }
        })
        .run(context)
        .expect("无法启动 Jenkins 预约部署");
}
