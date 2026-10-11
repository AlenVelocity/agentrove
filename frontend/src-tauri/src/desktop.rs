use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
#[cfg(unix)]
use std::os::unix::process::CommandExt;
#[cfg(windows)]
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

#[cfg(target_os = "macos")]
use objc2_app_kit::{NSWindow, NSWindowButton};
use rand::Rng;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::Manager;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[tauri::command]
fn get_backend_port(state: tauri::State<'_, Arc<OnceLock<Result<u16, String>>>>) -> Result<u16, String> {
    loop {
        if let Some(result) = state.get() {
            return result.clone();
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

fn data_dir() -> PathBuf {
    dirs::data_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join("com.agentrove.app")
}

fn ensure_secret_key(data_dir: &Path) -> String {
    fs::create_dir_all(data_dir).ok();
    let key_path = data_dir.join(".secret_key");
    if let Ok(key) = fs::read_to_string(&key_path) {
        let key = key.trim().to_string();
        if key.len() >= 32 {
            return key;
        }
    }
    let mut rng = rand::rng();
    let key: String = (0..32)
        .map(|_| format!("{:02x}", rng.random_range(0u8..=255)))
        .collect();
    fs::write(&key_path, &key).ok();
    key
}

// The file whose presence marks a usable sidecar: the bash launcher on Unix,
// the bundled interpreter on Windows (run_build.mjs writes no bash there).
#[cfg(not(windows))]
fn sidecar_entry(sidecar_dir: &Path) -> PathBuf {
    sidecar_dir.join("agentrove-backend")
}

#[cfg(windows)]
fn sidecar_entry(sidecar_dir: &Path) -> PathBuf {
    sidecar_dir.join("python").join("python.exe")
}

fn resolve_sidecar_dir(app_handle: &tauri::AppHandle) -> PathBuf {
    #[cfg(debug_assertions)]
    {
        let dev_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .join("backend-sidecar");
        if sidecar_entry(&dev_dir).exists() {
            return dev_dir;
        }
    }

    let resource_dir = app_handle
        .path()
        .resource_dir()
        .expect("failed to resolve resource dir");

    let nested = resource_dir.join("_up_").join("backend-sidecar");
    if sidecar_entry(&nested).exists() {
        return nested;
    }

    let direct = resource_dir.join("backend-sidecar");
    if sidecar_entry(&direct).exists() {
        return direct;
    }

    panic!(
        "Backend sidecar not found at {:?} or {:?}",
        nested, direct
    );
}

fn pick_available_port() -> u16 {
    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("failed to bind ephemeral port");
    listener.local_addr().unwrap().port()
}

fn backend_ready(port: u16) -> bool {
    let mut stream = match TcpStream::connect(("127.0.0.1", port)) {
        Ok(stream) => stream,
        Err(_) => return false,
    };
    let _ = stream.set_write_timeout(Some(Duration::from_secs(1)));
    let _ = stream.set_read_timeout(Some(Duration::from_secs(1)));
    let request =
        b"GET /api/v1/readyz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n";
    if stream.write_all(request).is_err() {
        return false;
    }
    let mut response = String::new();
    if stream.read_to_string(&mut response).is_err() {
        return false;
    }
    response.starts_with("HTTP/1.1 200") || response.starts_with("HTTP/1.0 200")
}

#[cfg(unix)]
fn kill_process_tree(child: &mut Child) {
    // spawn_backend put the sidecar in its own process group, so a negative
    // pid reaches uvicorn and every agent it spawned.
    let pid = child.id() as libc::pid_t;
    unsafe {
        libc::kill(-pid, libc::SIGKILL);
    }
}

#[cfg(windows)]
fn kill_process_tree(child: &mut Child) {
    // No process groups to signal on Windows; taskkill walks the tree by parent
    // pid so the interpreter, uvicorn and the agent processes go together.
    let _ = Command::new("taskkill")
        .args(["/PID", &child.id().to_string(), "/T", "/F"])
        .creation_flags(CREATE_NO_WINDOW)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

fn terminate_backend_process(backend: &Arc<Mutex<Option<Child>>>) {
    if let Ok(mut guard) = backend.lock() {
        if let Some(ref mut child) = *guard {
            kill_process_tree(child);
            let _ = child.wait();
        }
        *guard = None;
    }
}

fn pipe_output<R: Read + Send + 'static>(stream: R, is_err: bool) {
    std::thread::spawn(move || {
        let reader = BufReader::new(stream);
        for line in reader.lines().flatten() {
            if is_err {
                eprintln!("[backend] {}", line);
            } else {
                println!("[backend] {}", line);
            }
        }
    });
}

#[cfg(not(windows))]
fn backend_path_env(_sidecar_dir: &Path) -> String {
    // The bash launcher prepends the bundled bin dirs itself.
    let mut backend_path = std::env::var("PATH").unwrap_or_default();
    if let Some(home) = dirs::home_dir() {
        // OpenCode lives in ~/.opencode/bin; Tauri gives the backend an explicit PATH.
        backend_path.push_str(&format!(":{}/.opencode/bin", home.display()));
        backend_path.push_str(&format!(":{}/.local/bin", home.display()));
    }
    backend_path.push_str(":/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin");
    backend_path
}

#[cfg(windows)]
fn backend_path_env(sidecar_dir: &Path) -> String {
    // Same prefix the bash launcher adds on Unix: bundled rg, the ACP .cmd
    // launchers, bundled node, then whatever the user has. ~/.local/bin is
    // where Claude Code's native installer puts claude.exe.
    let mut parts = vec![
        sidecar_dir.join("bin").display().to_string(),
        sidecar_dir.join("node").display().to_string(),
        sidecar_dir.join("node_modules").join(".bin").display().to_string(),
    ];
    if let Some(home) = dirs::home_dir() {
        parts.push(home.join(".local").join("bin").display().to_string());
    }
    parts.push(std::env::var("PATH").unwrap_or_default());
    parts.join(";")
}

#[cfg(not(windows))]
fn backend_command(sidecar_dir: &Path) -> Command {
    let mut command = Command::new(sidecar_entry(sidecar_dir));
    unsafe {
        command.pre_exec(|| {
            if libc::setpgid(0, 0) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    command
}

#[cfg(windows)]
fn backend_command(sidecar_dir: &Path) -> Command {
    // Run the interpreter directly rather than through a .cmd so no console
    // window flashes and the pid we hold is the backend itself.
    let mut command = Command::new(sidecar_entry(sidecar_dir));
    command
        .arg(sidecar_dir.join("entry.py"))
        .env("PYTHONPATH", sidecar_dir)
        .creation_flags(CREATE_NO_WINDOW);
    command
}

fn spawn_backend(
    app_handle: &tauri::AppHandle,
    data_dir: &Path,
    secret_key: &str,
    port: u16,
) -> Child {
    let sidecar_dir = resolve_sidecar_dir(app_handle);
    let db_path = data_dir.join("agentrove.db").to_string_lossy().to_string();
    let mut command = backend_command(&sidecar_dir);
    command
        .env("SECRET_KEY", secret_key)
        .env("BASE_URL", format!("http://127.0.0.1:{port}"))
        .env("DATABASE_URL", format!("sqlite+aiosqlite:///{db_path}"))
        .env("PATH", backend_path_env(&sidecar_dir))
        .env(
            "STORAGE_PATH",
            data_dir.join("storage").to_string_lossy().to_string(),
        )
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let mut child = command.spawn().expect("failed to spawn backend process");

    if let Some(stdout) = child.stdout.take() {
        pipe_output(stdout, false);
    }
    if let Some(stderr) = child.stderr.take() {
        pipe_output(stderr, true);
    }

    child
}

fn show_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn build_tray(app: &tauri::App) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "show", "Show Agentrove", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit Agentrove", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &quit])?;

    TrayIconBuilder::new()
        .icon(app.default_window_icon().unwrap().clone())
        .tooltip("Agentrove")
        .menu(&menu)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => show_main_window(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .build(app)?;

    Ok(())
}

pub fn run() {
    let data_dir = data_dir();
    let secret_key = ensure_secret_key(&data_dir);
    let port = pick_available_port();
    let backend_port: Arc<OnceLock<Result<u16, String>>> = Arc::new(OnceLock::new());
    let backend_process: Arc<Mutex<Option<Child>>> = Arc::new(Mutex::new(None));
    let backend_for_exit = backend_process.clone();

    tauri::Builder::default()
        .plugin(tauri_plugin_store::Builder::new().build())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(backend_port.clone())
        .invoke_handler(tauri::generate_handler![get_backend_port])
        .on_window_event(|window, event| {
            // Close = hide to tray; real exit is tray Quit (app.exit bypasses this).
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .setup(move |app| {
            // Keep a titled overlay bar so minimize works (borderless can't on macOS);
            // hide native traffic lights — frontend draws custom ones.
            #[cfg(target_os = "macos")]
            if let Some(window) = app.get_webview_window("main") {
                if let Ok(ns_window) = window.ns_window() {
                    unsafe {
                        let ns_window = &*(ns_window as *const NSWindow);
                        for kind in [
                            NSWindowButton::CloseButton,
                            NSWindowButton::MiniaturizeButton,
                            NSWindowButton::ZoomButton,
                        ] {
                            if let Some(button) = ns_window.standardWindowButton(kind) {
                                button.setHidden(true);
                            }
                        }
                    }
                }
            }

            build_tray(app)?;

            let child = spawn_backend(app.handle(), &data_dir, &secret_key, port);
            *backend_process.lock().unwrap() = Some(child);

            let port_ref = backend_port.clone();
            std::thread::spawn(move || {
                for _ in 0..60 {
                    std::thread::sleep(Duration::from_millis(500));
                    if backend_ready(port) {
                        let _ = port_ref.set(Ok(port));
                        return;
                    }
                }
                eprintln!("[backend] readyz timeout");
                let _ = port_ref.set(Err(
                    "Backend failed readiness checks within startup timeout".to_string(),
                ));
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while running tauri application")
        .run(move |_app, event| {
            if matches!(
                event,
                tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit
            ) {
                terminate_backend_process(&backend_for_exit);
            }
        });
}
