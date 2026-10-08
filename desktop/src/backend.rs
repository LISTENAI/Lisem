use gpui::RenderImage;
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    io::{BufRead, BufReader, Write},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, ChildStdout, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread,
    time::Duration,
};

#[derive(Clone, Default, Deserialize)]
pub struct Device {
    pub id: String,
    pub name: String,
    pub path: String,
    pub uid: String,
    #[serde(default)]
    pub hardware: Value,
    #[serde(default)]
    pub host: Value,
    pub unavailable: Option<String>,
}

#[derive(Clone, Default)]
pub struct Snapshot {
    pub data: Value,
    pub frame: Option<Arc<RenderImage>>,
    pub frames: BTreeMap<String, Arc<RenderImage>>,
    pub selected: Option<String>,
    pub message: String,
    pub error: Option<String>,
    pub busy: bool,
    pub operation: Option<(String, Value)>,
}

pub enum Request {
    Call(&'static str, Value),
    Quit,
}

pub struct Backend {
    sender: mpsc::Sender<Request>,
    pub snapshot: Arc<Mutex<Snapshot>>,
    worker: Mutex<Option<thread::JoinHandle<()>>>,
}

impl Backend {
    pub fn start(root: PathBuf, data: PathBuf, runtime: PathBuf) -> Arc<Self> {
        let (sender, receiver) = mpsc::channel();
        let snapshot = Arc::new(Mutex::new(Snapshot::default()));
        let output = snapshot.clone();
        let worker = thread::spawn(move || {
            let mut bridge = match Bridge::new(&root, &data, &runtime) {
                Ok(value) => value,
                Err(error) => {
                    output.lock().unwrap().error = Some(error);
                    return;
                }
            };
            let frame_stop = Arc::new(AtomicBool::new(false));
            let frame_output = output.clone();
            let stop = frame_stop.clone();
            let frames = thread::spawn(move || {
                let mut displays = BTreeMap::<String, crate::display::Display>::new();
                while !stop.load(Ordering::Acquire) {
                    let paths: BTreeMap<String, PathBuf> =
                        frame_output.lock().unwrap().data["sessions"]
                            .as_object()
                            .into_iter()
                            .flatten()
                            .filter_map(|(id, session)| {
                                Some((id.clone(), PathBuf::from(session["framebuffer"].as_str()?)))
                            })
                            .collect();
                    displays.retain(|id, _| paths.contains_key(id));
                    for (id, path) in paths {
                        if displays.get(&id).map(|d| &d.path) != Some(&path) {
                            displays.remove(&id);
                            if let Ok(display) = crate::display::Display::open(&path) {
                                displays.insert(id.clone(), display);
                            }
                        }
                        if let Some(display) = displays.get_mut(&id)
                            && let Some(frame) = display.next()
                        {
                            let mut state = frame_output.lock().unwrap();
                            if state.data["sessions"][&id]["framebuffer"]
                                .as_str()
                                .map(Path::new)
                                == Some(path.as_path())
                            {
                                state.frames.insert(id, frame);
                            }
                        }
                    }
                    thread::sleep(Duration::from_millis(16));
                }
            });
            loop {
                match receiver.recv_timeout(Duration::from_millis(250)) {
                    Ok(Request::Quit) | Err(mpsc::RecvTimeoutError::Disconnected) => break,
                    Ok(Request::Call(method, params)) => {
                        {
                            let mut state = output.lock().unwrap();
                            state.busy = true;
                            state.error = None;
                        }
                        output.lock().unwrap().operation = Some((method.into(), params.clone()));
                        let reply = bridge.call(method, params);
                        let mut state = output.lock().unwrap();
                        state.busy = false;
                        state.operation = None;
                        match reply {
                            Ok(value) => {
                                if method == "create" || method == "attach" {
                                    state.selected = value["id"].as_str().map(str::to_owned);
                                }
                                state.message = match method {
                                    "rename" => "名称已保存".into(),
                                    "settings" | "sound" => String::new(),
                                    "detach" => "已从实例库移除，文件保留".into(),
                                    "create" => "实例已创建".into(),
                                    "attach" => "实例已打开".into(),
                                    "import" => "LPK 已写入，设备身份保持不变。".into(),
                                    "erase" => "Flash 已清空".into(),
                                    "regenerate_uid" => "UID 已重新生成".into(),
                                    "audio" => "语音已送入输入队列。".into(),
                                    "button" => String::new(),
                                    "serial" if value.is_null() => "串口已关闭".into(),
                                    "serial" => format!(
                                        "串口已就绪：{}",
                                        value.as_str().unwrap_or_default()
                                    ),
                                    "stop" | "start" | "reset" => String::new(),
                                    _ => String::new(),
                                };
                            }
                            Err(error) => state.error = Some(error),
                        }
                    }
                    Err(mpsc::RecvTimeoutError::Timeout) => {}
                }
                match bridge.call("status", json!({})) {
                    Ok(data) => {
                        let mut state = output.lock().unwrap();
                        let previous = state.data["sessions"].clone();
                        state.frames.retain(|id, _| {
                            data["sessions"][id]["output"].is_string()
                                && data["sessions"][id]["output"] == previous[id]["output"]
                        });
                        state.data = data;
                    }
                    Err(error) => {
                        let mut state = output.lock().unwrap();
                        state.error = Some(error);
                        state.busy = false;
                        break;
                    }
                }
            }
            frame_stop.store(true, Ordering::Release);
            let _ = frames.join();
        });
        Arc::new(Self {
            sender,
            snapshot,
            worker: Mutex::new(Some(worker)),
        })
    }

    pub fn send(&self, method: &'static str, params: Value) {
        {
            let mut state = self.snapshot.lock().unwrap();
            state.busy = true;
            state.operation = Some((method.into(), params.clone()));
        }
        if self.sender.send(Request::Call(method, params)).is_err() {
            let mut state = self.snapshot.lock().unwrap();
            state.busy = false;
            state.error = Some("控制服务已退出，请重新打开应用。".into());
        }
    }

    pub fn request_shutdown(&self) {
        let _ = self.sender.send(Request::Quit);
    }

    pub fn shutdown(&self) {
        self.request_shutdown();
        if let Some(worker) = self.worker.lock().unwrap().take() {
            let _ = worker.join();
        }
    }
}

struct Bridge {
    child: Child,
    input: Option<ChildStdin>,
    output: BufReader<ChildStdout>,
    next: u64,
}

impl Bridge {
    fn new(root: &Path, data: &Path, runtime: &Path) -> Result<Self, String> {
        let mut child = lisem_core::process::command(runtime)
            .arg("--root")
            .arg(root)
            .arg("--data-dir")
            .arg(data)
            .arg("_bridge")
            .current_dir(root)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .map_err(|e| format!("无法启动控制服务：{e}"))?;
        let input = child.stdin.take();
        let output = BufReader::new(child.stdout.take().unwrap());
        Ok(Self {
            child,
            input,
            output,
            next: 0,
        })
    }

    fn call(&mut self, method: &str, params: Value) -> Result<Value, String> {
        self.next += 1;
        let request = json!({"id":self.next,"method":method,"params":params});
        writeln!(self.input.as_mut().unwrap(), "{request}").map_err(|e| e.to_string())?;
        let mut line = String::new();
        if self
            .output
            .read_line(&mut line)
            .map_err(|e| e.to_string())?
            == 0
        {
            return Err("控制服务已退出。请检查运行环境或设备库是否已被另一个窗口打开。".into());
        }
        let reply: Value =
            serde_json::from_str(&line).map_err(|e| format!("控制服务响应异常：{e}"))?;
        if reply["id"].as_u64() != Some(self.next) {
            return Err("控制服务响应序号不匹配".into());
        }
        if let Some(error) = reply["error"].as_str() {
            return Err(error.into());
        }
        Ok(reply["result"].clone())
    }
}

impl Drop for Bridge {
    fn drop(&mut self) {
        // EOF makes the service stop its own simulator process group and PTYs.
        self.input.take();
        let _ = self.child.wait();
    }
}
