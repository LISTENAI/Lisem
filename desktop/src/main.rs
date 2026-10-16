#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

mod about;
mod updates;
mod backend;
mod camera_selector;
mod display;
mod icons;
mod library;
mod peripheral_controls;
mod platform;
mod toolbar;
mod signals;
mod theme;
mod touch;
mod wifi_environment;

use backend::{Backend, Device, Snapshot};
use camera_selector::{CameraChoice, CameraOption};
use gpui::component::{
    ActiveTheme, Disableable, Icon,
    scroll::ScrollableElement,
    button::{Button, ButtonVariants},
    input::{Input, InputState},
    menu::{DropdownMenu, PopupMenu, PopupMenuItem},
    select::{Select, SelectEvent, SelectItem, SelectState},
    switch::Switch,
    tab::{Tab, TabBar},
    tooltip::Tooltip,
};
use gpui::{
    AnyWindowHandle, App, Bounds, ClipboardItem, Context, Div, Entity, KeyBinding, Menu, MenuItem,
    MouseButton, PathPromptOptions, PromptLevel, SharedString, TitlebarOptions, WeakEntity, Window,
    WindowBounds, WindowOptions, actions, div, img, prelude::*, px, rgb, size,
};
use peripheral_controls::settings_row;
use platform::TitleBar;
use library::BoardOption;
use serde_json::{Value, json};
use std::{
    cell::RefCell,
    collections::{HashMap, HashSet},
    path::PathBuf,
    rc::Rc,
    sync::Arc,
    time::Duration,
};

actions!(
    lisa_sim,
    [Quit, ShowLibrary, CloseWindow, InstallCli, ShowAbout]
);

#[derive(Clone)]
enum Mode {
    Library,
    Device(String),
}
#[derive(Clone)]
struct ManagedWindow {
    handle: AnyWindowHandle,
    view: WeakEntity<Desktop>,
}
#[derive(Default)]
struct Windows {
    library: Option<ManagedWindow>,
    about: Option<AnyWindowHandle>,
    devices: HashMap<String, ManagedWindow>,
}
type WindowBook = Rc<RefCell<Windows>>;

#[derive(Clone, Copy)]
enum Action {
    New,
    Cancel,
    Details,
    Settings,
    WifiSettings,
    Signals,
    SaveName,
    Detach,
    Open,
    RegenerateUid,
    Reset,
    ResetDownload,
    ChoosePackage,
    ClearPackage,
    Create,
    Attach,
    Import,
    ImportBoard,
    Erase,
    Start,
    Stop,
    Camera,
    ClearCamera,
    RefreshCameras,
    MountSd,
    EjectSd,
    Sound,
    Network,
    Microphone,
    CopySerialPath(u8),
    SerialToggle(u8),
    SerialInfo,
    UsbAdb,
    CopyUsbAddress,
    Folder,
    CopyUid,
}

struct Desktop {
    backend: Arc<Backend>,
    windows: WindowBook,
    mode: Mode,
    state: Snapshot,
    selected: Option<String>,
    notice: String,
    creating: bool,
    settings: bool,
    wifi_visible: bool,
    signals_visible: bool,
    signals: Option<Entity<signals::Signals>>,
    wifi: Option<Entity<wifi_environment::WifiEnvironment>>,
    settings_tab: &'static str,
    held: HashSet<String>,
    touch: Option<Value>,
    new_board: Option<String>,
    board_select: Entity<SelectState<Vec<BoardOption>>>,
    board_options: Vec<BoardOption>,
    package: Option<PathBuf>,
    name: Entity<InputState>,
    title: String,
    camera_select: Entity<SelectState<Vec<CameraOption>>>,
    camera_options: Vec<CameraOption>,
    rendered_frame: Option<Arc<gpui::RenderImage>>,
}

impl Desktop {
    fn install_cli(&mut self, cx: &mut Context<Self>) {
        self.notice = "正在安装命令行工具…".into();
        cx.notify();
        cx.spawn(async move |view, cx| {
            let result = cx
                .background_executor()
                .spawn(async { lisem_core::install::install() })
                .await;
            let _ = view.update(cx, |view, cx| {
                view.notice = match result {
                    Ok(_) if cfg!(windows) => "已安装命令行工具，重新打开终端即可使用".into(),
                    Ok(result) if result.path_ready => {
                        format!("已安装命令行工具：{}", result.command.display())
                    }
                    Ok(result) => format!(
                        "已安装 {}；请将 {} 加入终端 PATH",
                        result.command.display(),
                        result.command.parent().unwrap().display()
                    ),
                    Err(error) => format!("CLI 安装失败：{error:#}"),
                };
                cx.notify();
            });
        })
        .detach();
    }
    fn new(
        backend: Arc<Backend>,
        windows: WindowBook,
        mode: Mode,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) -> Self {
        cx.spawn(async move |view, cx| {
            loop {
                cx.background_executor()
                    .timer(Duration::from_millis(16))
                    .await;
                if view
                    .update(cx, |view, cx| {
                        let mut state = view.backend.snapshot.lock().unwrap().clone();
                        if matches!(view.mode, Mode::Library) {
                            if let Some(id) = state.selected.take() {
                                view.selected = Some(id);
                                view.creating = false;
                                view.backend.snapshot.lock().unwrap().selected = None;
                            }
                        }
                        if matches!(view.mode, Mode::Library) { state.for_device(backend::LIBRARY_NOTICE); }
                        if let Some(id) = &view.selected {
                            if matches!(view.mode, Mode::Device(_)) { state.for_device(id); }
                            state.data["session"] = state.data["sessions"][id].clone();
                            state.frame = state.frames.get(id).cloned();
                        }
                        let changed = view.state.data != state.data
                            || view.state.cameras != state.cameras
                            || view.state.frame.as_ref().map(Arc::as_ptr)
                                != state.frame.as_ref().map(Arc::as_ptr)
                            || view.state.busy != state.busy
                            || view.state.error != state.error
                            || view.state.message != state.message
                            || view.state.operation != state.operation
                            || view.state.pending != state.pending;
                        view.state = state;
                        if matches!(view.mode, Mode::Library)
                            && !view
                                .devices()
                                .iter()
                                .any(|d| Some(&d.id) == view.selected.as_ref())
                        {
                            view.selected = view.devices().first().map(|d| d.id.clone());
                            view.settings = false;
                        }
                        if changed {
                            cx.notify();
                        }
                    })
                    .is_err()
                {
                    break;
                }
            }
        })
        .detach();
        let selected = match &mode {
            Mode::Device(id) => Some(id.clone()),
            Mode::Library => None,
        };
        let board_select = cx.new(|cx| SelectState::new(Vec::<BoardOption>::new(), None, window, cx));
        cx.subscribe_in(&board_select, window, |view, _, event, _, cx| {
            let SelectEvent::Confirm(choice) = event;
            view.new_board = choice.clone();
            cx.notify();
        }).detach();
        let camera_select =
            cx.new(|cx| SelectState::new(Vec::<CameraOption>::new(), None, window, cx));
        cx.subscribe_in(&camera_select, window, |view, _, event, window, cx| {
            let SelectEvent::Confirm(Some(choice)) = event else {
                return;
            };
            view.choose_camera(choice, window, cx);
            // A picker or pending backend request does not change the confirmed source.
            cx.notify();
        })
        .detach();
        let mut state = backend.snapshot.lock().unwrap().clone();
        if let Some(id) = &selected {
            state.data["session"] = state.data["sessions"][id].clone();
            state.frame = state.frames.get(id).cloned();
        }
        let mut view = Self {
            backend,
            windows,
            mode,
            state,
            selected,
            notice: String::new(),
            creating: false,
            settings: false,
            wifi_visible: false,
            signals_visible: false,
            signals: None,
            wifi: None,
            settings_tab: "general",
            held: HashSet::new(),
            touch: None,
            new_board: None,
            board_select,
            board_options: Vec::new(),
            package: None,
            name: cx.new(|cx| InputState::new(window, cx).placeholder("实例名称")),
            title: String::new(),
            camera_select,
            camera_options: Vec::new(),
            rendered_frame: None,
        };
        if matches!(view.mode, Mode::Device(_)) {
            view.refresh_cameras();
        }
        view
    }

    fn refresh_cameras(&mut self) {
        if self.state.data["capabilities"]["host_camera"] == true
            && self.device().is_some_and(|device| device.capabilities.camera)
        {
            self.send("camera_devices", json!({"id": self.selected}));
        }
    }

    fn devices(&self) -> Vec<Device> {
        serde_json::from_value(self.state.data["devices"].clone()).unwrap_or_default()
    }
    fn device(&self) -> Option<Device> {
        self.devices()
            .into_iter()
            .find(|d| Some(&d.id) == self.selected.as_ref())
    }
    fn running(&self) -> bool {
        self.state.data["session"].is_object() && self.state.data["session"]["finished"] == false
    }
    fn active(&self) -> bool {
        self.state.data["session"]["device_id"].as_str() == self.selected.as_deref()
            && self.state.data["session"].is_object()
    }
    fn powered(&self) -> bool {
        self.active() && self.running()
    }
    fn params(&self) -> Value {
        json!({"id":self.selected, "run":self.state.data["session"]["output"]})
    }
    fn lifecycle(&self) -> &'static str {
        if let Some((op, params)) = &self.state.operation {
            if params["id"].as_str() == self.selected.as_deref() {
                match op.as_str() {
                    "start" => return "正在上电",
                    "stop" => return "正在下电",
                    "reset" | "reset_download" => return "正在复位",
                    _ => {}
                }
            }
        }
        if !self.active() {
            return "已下电";
        }
        match self.state.data["session"]["lifecycle"].as_str() {
            Some("starting") => "正在上电",
            Some("resetting") => "正在复位",
            Some("stopping") => "正在下电",
            Some("failed") => "启动失败",
            Some("on") => {
                if self.state.data["session"]["guest_fault"] == true {
                    "固件异常"
                } else {
                    "已上电"
                }
            }
            _ => "已下电",
        }
    }
    fn control_busy(&self, methods: &[&str]) -> bool {
        let state = self.backend.snapshot.lock().unwrap();
        state.pending.iter().any(|(method, params)| {
            let same_device = params["id"].as_str() == self.selected.as_deref();
            same_device
                && (methods.contains(&method.as_str())
                    || matches!(method.as_str(),
                        "start" | "stop" | "reset" | "reset_download"
                        | "import" | "erase" | "regenerate_uid"))
        })
    }
    fn serial_busy(&self, channel: u8) -> bool {
        self.control_busy(&[])
            || self.backend.snapshot.lock().unwrap().pending.iter()
                .any(|(method, params)| {
                    method == "serial"
                        && params["id"].as_str() == self.selected.as_deref()
                        && params["channel"] == channel
                })
    }
    fn send(&mut self, method: &'static str, params: Value) {
        self.notice.clear();
        self.backend.send(method, params);
        self.state.busy = true;
    }
    fn show_settings(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        self.release_buttons();
        if self.settings {
            return;
        }
        self.wifi_visible = false;
        self.signals_visible = false;
        self.settings = true;
        self.refresh_cameras();
        if let Some(d) = self.device() {
            self.name
                .update(cx, |name, cx| name.set_value(&d.name, window, cx));
        }
    }
    fn choose_file(&mut self, action: Action, cx: &mut Context<Self>) {
        let selected = self.selected.clone();
        let run = if self.powered() {
            self.state.data["session"]["output"].clone()
        } else {
            Value::Null
        };
        let directory = matches!(action, Action::Attach);
        let picker = cx.prompt_for_paths(PathPromptOptions {
            files: !directory,
            directories: directory,
            multiple: false,
            prompt: Some(
                match action {
                    Action::Attach => "打开实例目录",
                    Action::Camera => "选择摄像头图片（PNG / JPEG / PNM）",
                    Action::MountSd => "选择 SD 卡镜像",
                    Action::ImportBoard => "选择板型 JSON",
                    _ => "选择要写入 Flash 的 LPK",
                }
                .into(),
            ),
        });
        cx.spawn(async move |view, cx| match picker.await {
            Ok(Ok(Some(paths))) => {
                if let Some(path) = paths.first() {
                    let _ = view.update(cx, |v, cx| {
                        match action {
                            Action::ChoosePackage => v.package = Some(path.clone()),
                            Action::Attach => v.send("attach", json!({"path":path})),
                            Action::ImportBoard => v.send("board_import", json!({"path":path})),
                            Action::Camera => {
                                v.send("camera", json!({"id":selected,"run":run,"path":path}))
                            }
                            Action::MountSd => v.send("sd_card", json!({
                                "id":selected,"run":run,"medium":{"path":path}
                            })),
                            _ => v.send("import", json!({"id":selected,"package":path})),
                        }
                        cx.notify();
                    });
                }
            }
            Ok(Err(error)) => {
                let _ = view.update(cx, |v, cx| {
                    v.notice = error.to_string();
                    cx.notify();
                });
            }
            _ => {}
        })
        .detach();
    }
    fn act(&mut self, action: Action, window: &mut Window, cx: &mut Context<Self>) {
        self.notice.clear();
        match action {
            Action::New => {
                self.release_buttons();
                self.creating = true;
                self.settings = false;
                self.package = None;
                self.new_board = self.state.data["boards"].as_array()
                    .and_then(|v| v.first()).and_then(|v| v["id"].as_str()).map(str::to_owned);
                self.name.update(cx, |name, cx| name.set_value("", window, cx));
            }
            Action::Cancel => {
                self.creating = false;
                self.settings = false;
                self.wifi_visible = false;
            }
            Action::Details => {}
            Action::Settings if matches!(self.mode, Mode::Library) => {
                if let Some(d) = self.device() {
                    let handle = show_device(&d, self.backend.clone(), self.windows.clone(), cx);
                    let _ = handle.handle.update(cx, |_, window, cx| {
                        let _ = handle.view.update(cx, |view, cx| {
                            view.show_settings(window, cx);
                            cx.notify();
                        });
                    });
                }
            }
            Action::Settings => self.show_settings(window, cx),
            Action::Signals => {
                if let Some(d) = self.device() {
                    self.release_buttons();
                    self.settings = false;
                    self.wifi_visible = false;
                    self.signals_visible = !self.signals_visible;
                    if self.signals.is_none() {
                        self.signals = Some(cx.new(|cx| {
                            signals::Signals::new(self.backend.clone(), d, window, cx)
                        }));
                    }
                }
            }
            Action::WifiSettings => {
                if let Some(d) = self.device() {
                    self.release_buttons();
                    self.settings = false;
                    self.signals_visible = false;
                    self.wifi_visible = true;
                    if self.wifi.is_none() {
                        self.wifi = Some(cx.new(|cx| wifi_environment::WifiEnvironment::new(
                            self.backend.clone(), d, window, cx)));
                    }
                }
            }
            Action::SaveName => self.send("rename", json!({
                "id": self.selected, "name": self.name.read(cx).value()
            })),
            Action::Open => {
                if let Some(d) = self.device() {
                    show_device(&d, self.backend.clone(), self.windows.clone(), cx);
                }
            }
            Action::ChoosePackage | Action::Attach | Action::Import | Action::ImportBoard | Action::Camera | Action::MountSd => {
                self.choose_file(action, cx);
            }
            Action::RefreshCameras => self.refresh_cameras(),
            Action::ClearCamera => {
                let mut params = self.params();
                params["path"] = Value::Null;
                self.send("camera", params);
            }
            Action::EjectSd => {
                let mut params = if self.powered() { self.params() } else { json!({"id":self.selected}) };
                params["medium"] = Value::Null;
                self.send("sd_card", params);
            }
            Action::ClearPackage => self.package = None,
            Action::Create => self.send("create", json!({
                "name": self.name.read(cx).value(), "board": self.new_board, "package": self.package
            })),
            Action::Start => self.send("start", json!({
                "id": self.selected,
                "online": self.device().is_some_and(|d| d.capabilities.wifi),
                "seconds": 86400,
                "timeout": 172800,
            })),
            Action::Reset | Action::ResetDownload | Action::Stop => {
                self.release_buttons();
                let method = match action {
                    Action::Reset => "reset",
                    Action::ResetDownload => "reset_download",
                    _ => "stop",
                };
                self.send(method, self.params());
            }
            Action::Sound => {
                if let Some(d) = self.device() {
                    self.send("sound", json!({"id": d.id, "enabled": d.host["sound"] != true}));
                }
            }
            Action::Network => {
                if let Some(d) = self.device() {
                    let mut params = if self.powered() { self.params() } else { json!({"id":d.id}) };
                    params["enabled"] = json!(!self.access_point_present(&d));
                    self.send("access_point", params);
                }
            }
            Action::Microphone => {
                if let Some(d) = self.device() {
                    let mut params = if self.powered() {
                        self.params()
                    } else {
                        json!({"id": d.id})
                    };
                    params["enabled"] = json!(!self.host_microphone_enabled(&d));
                    self.send("microphone", params);
                }
            }
            Action::SerialToggle(channel) => {
                let enabled = self.serial_path(channel).is_none();
                self.send("serial", json!({"id": self.selected, "channel": channel, "enabled": enabled}));
            }
            Action::UsbAdb => {
                if self.usb_change_enabled() {
                    let mut params = self.params();
                    params["enabled"] = json!(!self.usb_enabled());
                    self.send("usb_adb", params);
                }
            }
            Action::CopyUsbAddress => {
                if let Some(address) = self.usb_address() {
                    cx.write_to_clipboard(ClipboardItem::new_string(address.to_owned()));
                    self.notice = "连接地址已复制".into();
                }
            }
            Action::SerialInfo => self.show_serial_info(window, cx),
            Action::CopySerialPath(channel) => {
                if let Some(path) = self.serial_path(channel) {
                    cx.write_to_clipboard(ClipboardItem::new_string(path.to_owned()));
                    self.notice = "连接路径已复制".into();
                }
            }
            Action::Folder => {
                if let Some(d) = self.device() {
                    cx.open_with_system(&PathBuf::from(d.path));
                }
            }
            Action::CopyUid => {
                if let Some(d) = self.device() {
                    cx.write_to_clipboard(ClipboardItem::new_string(d.uid));
                    self.notice = "UID 已复制".into();
                }
            }
            Action::Erase | Action::RegenerateUid | Action::Detach => {
                if let Some(d) = self.device() {
                    let (title, detail, confirm, method) = match action {
                        Action::RegenerateUid => (
                            "重新生成 UID？", "设备 UID 将改变，Flash 和其余 OTP 保留。",
                            "重新生成", "regenerate_uid"
                        ),
                        Action::Detach => (
                            "从实例库移除？", "实例文件会保留，可随时通过「打开实例」重新加入。",
                            "移除", "detach"
                        ),
                        _ => ("清空 Flash？", "固件和业务数据将被清空。OTP 和 UID 保留。", "清空", "erase"),
                    };
                    let answer = window.prompt(PromptLevel::Warning, title, Some(detail), &["取消", confirm], cx);
                    cx.spawn(async move |view, cx| {
                        if answer.await == Ok(1) {
                            let _ = view.update(cx, |v, cx| {
                                v.send(method, json!({"id": d.id, "confirm_uid": d.uid}));
                                v.settings = false;
                                cx.notify();
                            });
                        }
                    }).detach();
                }
            }
        }
        cx.notify();
    }
    fn button_state(&mut self, id: &str, pressed: bool) {
        let changed = if pressed {
            self.held.insert(id.into())
        } else {
            self.held.remove(id)
        };
        if changed && self.powered() {
            let mut p = self.params();
            p["button"] = json!(id);
            p["pressed"] = json!(pressed);
            self.backend.send("button", p);
        }
    }
    fn release_touch(&mut self) {
        if let Some(mut params) = self.touch.take() {
            params["pressed"] = json!(false);
            self.backend.send("touch", params);
        }
    }
    fn touch_at(&mut self, x: u32, y: u32) {
        let mut params = self.touch.clone().unwrap_or_else(|| self.params());
        params["x"] = json!(x);
        params["y"] = json!(y);
        params["pressed"] = json!(true);
        if self.touch.as_ref() != Some(&params) {
            self.backend.send("touch", params.clone());
            self.touch = Some(params);
        }
    }
    fn release_buttons(&mut self) {
        self.release_touch();
        for id in self.held.clone() {
            self.button_state(&id, false);
        }
    }
    fn serial_path(&self, channel: u8) -> Option<&str> {
        let identifier = self.selected.as_deref()?;
        self.state.data["serial"][identifier][channel.to_string()].as_str()
    }
    fn menu_items(&self, action: Action) -> Vec<(String, Action, bool, bool)> {
        let idle = !self.control_busy(&[]);
        let on = self.powered();
        match action {
            _ => vec![
                ("实例设置…".into(), Action::Settings, true, false),
                (
                    "信号输入…".into(),
                    Action::Signals,
                    self.device()
                        .is_some_and(|d| d.capabilities.signals && d.inputs.as_array().is_some_and(|v| !v.is_empty())),
                    false,
                ),
                ("复制 UID".into(), Action::CopyUid, true, false),
                (
                    "复位到烧录模式".into(),
                    Action::ResetDownload,
                    idle && on
                        && self
                            .device()
                            .is_some_and(|d| d.capabilities.download),
                    false,
                ),
                (
                    "从 LPK 写入 Flash…".into(),
                    Action::Import,
                    idle && !on,
                    false,
                ),
            ],
        }
    }
    fn icon(
        &self,
        id: &'static str,
        label: &'static str,
        action: Action,
        enabled: bool,
        lit: bool,
        cx: &mut Context<Self>,
    ) -> gpui::AnyElement {
        let button = Button::new(id)
            .w(px(32.))
            .h(px(32.))
            .ghost()
            .icon(Icon::empty().path(id))
            .accessibility_label(label)
            .tooltip(label)
            .disabled(!enabled)
            .when(lit, |b| b.text_color(cx.theme().primary))
            .when(id == "power" && lit, |b| {
                b.primary().text_color(cx.theme().primary_foreground)
            })
            .when(id == "power", |b| {
                b.loading(matches!(
                    self.lifecycle(),
                    "正在上电" | "正在下电" | "正在复位"
                ))
                .when(
                    matches!(self.lifecycle(), "启动失败" | "固件异常"),
                    |b| b.danger().text_color(cx.theme().danger_foreground),
                )
            });
        if matches!(action, Action::Details) {
            let view = cx.entity().downgrade();
            button
                .dropdown_menu_with_anchor(gpui::Anchor::TopRight, move |mut menu, _, cx| {
                    let items = view
                        .upgrade()
                        .map(|v| v.read(cx).menu_items(action))
                        .unwrap_or_default();
                    for (label, action, enabled, checked) in items {
                        let view = view.clone();
                        menu = menu.item(
                            PopupMenuItem::new(label)
                                .disabled(!enabled)
                                .checked(checked)
                                .on_click(move |_, window, cx| {
                                    let _ = view.update(cx, |v, cx| v.act(action, window, cx));
                                }),
                        );
                    }
                    menu
                })
                .into_any_element()
        } else {
            button
                .on_click(cx.listener(move |v, _, w, cx| v.act(action, w, cx)))
                .into_any_element()
        }
    }
    fn control(
        &self,
        label: &str,
        id: &'static str,
        action: Action,
        enabled: bool,
        primary: bool,
        cx: &mut Context<Self>,
    ) -> Button {
        Button::new(id)
            .label(label.to_owned())
            .disabled(!enabled)
            .when(primary, |b| b.primary())
            .on_click(cx.listener(move |v, _, w, cx| v.act(action, w, cx)))
    }
    fn new_panel(&self, cx: &mut Context<Self>) -> impl IntoElement {
        let mut form = div()
            .flex()
            .flex_col()
            .gap_5()
            .max_w(px(520.))
            .w_full()
            .child(field("名称", self.name.clone()));
        form = form.child(div().flex().flex_col().gap_2()
            .child(caption("板型"))
            .child(div().flex().items_center().gap_3()
                .child(div().flex_1().min_w_0().child(
                    Select::new(&self.board_select).accessibility_label("板型").w_full().disabled(self.state.busy)))
                .child(self.control("导入板型…", "import-board", Action::ImportBoard,
                    !self.state.busy, false, cx))))
            .child(
                div()
                    .flex()
                    .flex_col()
                    .gap_2()
                    .child(caption("写入 Flash · 可选"))
                    .child(
                        self.control(
                            self.package
                                .as_ref()
                                .and_then(|p| p.file_name())
                                .and_then(|n| n.to_str())
                                .unwrap_or("选择 LPK…"),
                            "new-package",
                            Action::ChoosePackage,
                            !self.state.busy,
                            false,
                            cx,
                        ),
                    )
                    .when(self.package.is_some(), |e| {
                        e.child(self.control(
                            "使用空白 Flash",
                            "clear-package",
                            Action::ClearPackage,
                            true,
                            false,
                            cx,
                        ))
                    }),
            );
        div()
            .flex_1()
            .min_h_0()
            .flex()
            .flex_col()
            .child(
                div()
                    .id("new-instance-form")
                    .flex_1()
                    .min_h_0()
                    .flex()
                    .flex_col()
                    .items_center()
                    .p_6()
                    .child(form.flex_shrink_0())
                    .overflow_y_scrollbar(),
            )
            .child(
                div()
                    .flex_shrink_0()
                    .flex()
                    .justify_center()
                    .px_6()
                    .py_4()
                    .border_t_1()
                    .border_color(cx.theme().border)
                    .child(
                        div()
                            .w_full()
                            .max_w(px(520.))
                            .flex()
                            .justify_end()
                            .child(self.control(
                                "创建实例",
                                "create",
                                Action::Create,
                                !self.state.busy && self.new_board.is_some(),
                                true,
                                cx,
                            )),
                    ),
            )
    }
    fn settings_panel(&mut self, d: &Device, _window: &mut Window, cx: &mut Context<Self>) -> Div {
        let idle = !self.control_busy(&[]);
        let mutable = !self.running() && idle;
        let tabs = TabBar::new("settings-tabs")
            .underline()
            .selected_index(match self.settings_tab {
                "connections" => 1,
                "storage" => 2,
                _ => 0,
            })
            .child(Tab::new().label("常规"))
            .child(Tab::new().label("连接与媒体"))
            .child(Tab::new().label("身份与存储"))
            .on_click(cx.listener(|v, index: &usize, _, cx| {
                v.settings_tab = ["general", "connections", "storage"][*index];
                cx.notify();
            }));
        let mut body = div().w_full().max_w(px(700.)).flex().flex_col().gap_3();
        match self.settings_tab {
            "connections" => {
                body = body.child(self.media_settings(d, cx));
            }
            "storage" => {
                body = body
                    .child(settings_row(
                        "芯片 UID",
                        div().flex().flex_col().gap_3().child(d.uid.clone()).child(
                            div()
                                .flex()
                                .gap_2()
                                .child(self.control(
                                    "复制",
                                    "copy-uid",
                                    Action::CopyUid,
                                    true,
                                    false,
                                    cx,
                                ))
                                .child(self.control(
                                    "重新生成…",
                                    "regen",
                                    Action::RegenerateUid,
                                    mutable,
                                    false,
                                    cx,
                                )),
                        ),
                    ))
                    .child(settings_row(
                        "Flash",
                        div()
                            .flex()
                            .flex_col()
                            .gap_3()
                            .child(format!(
                                "{} MiB",
                                d.hardware["board"]["flash_bytes"].as_u64().unwrap_or(0) / 1048576
                            ))
                            .child(
                                div()
                                    .flex()
                                    .gap_2()
                                    .child(self.control(
                                        "从 LPK 写入…",
                                        "import",
                                        Action::Import,
                                        mutable,
                                        false,
                                        cx,
                                    ))
                                    .child(self.control(
                                        "清空 Flash…",
                                        "erase",
                                        Action::Erase,
                                        mutable,
                                        false,
                                        cx,
                                    )),
                            ),
                    ))
                    .when(self.running(), |body| {
                        body.child(caption("下电后可修改 UID 和 Flash"))
                    });
            }
            _ => {
                body = body
                    .child(settings_row(
                        "名称",
                        div()
                            .flex()
                            .items_center()
                            .gap_3()
                            .child(div().flex_1().min_w_0().child(Input::new(&self.name)))
                            .child(self.control(
                                "保存",
                                "save-name",
                                Action::SaveName,
                                idle,
                                false,
                                cx,
                            )),
                    ))
                    .child(settings_row(
                        "板型",
                        div().child(
                            d.hardware["board"]["name"]
                                .as_str()
                                .unwrap_or("不可用")
                                .to_owned(),
                        ),
                    ))
                    .child(settings_row(
                        "芯片",
                        div().child(
                            d.hardware["chip"]["name"]
                                .as_str()
                                .unwrap_or("不可用")
                                .to_owned(),
                        ),
                    ))
                    .child(settings_row(
                        "实例位置",
                        div()
                            .flex()
                            .flex_col()
                            .items_start()
                            .gap_3()
                            .child(div().id("instance-path").text_sm().truncate().child(d.path.clone())
                                .tooltip({ let path = d.path.clone(); move |w, cx| Tooltip::new(path.clone()).build(w, cx) }))
                            .child(self.control(
                                "打开所在文件夹",
                                "settings-folder",
                                Action::Folder,
                                true,
                                false,
                                cx,
                            )),
                    ));
            }
        }
        div()
            .flex_1()
            .min_w_0()
            .flex()
            .flex_col()
            .p_6()
            .gap_5()
            .child(tabs)
            .child(
                div()
                    .id("settings-body")
                    .flex_1()
                    .min_h_0()
                    .child(body)
                    .overflow_y_scrollbar(),
            )
    }
    fn device_overview(&self, d: &Device, cx: &mut Context<Self>) -> gpui::Stateful<Div> {
        let board = d.hardware["board"]["name"].as_str().unwrap_or_default();
        let chip = d.hardware["chip"]["name"].as_str().unwrap_or_default();
        let mut rows = div().flex().flex_col().w_full();
        for channel in d.capabilities.uart_channels.iter().copied() {
            let path = self.serial_path(channel).map(str::to_owned);
            let ready = path.is_some();
            let endpoint = path.unwrap_or_else(|| "未启用".into());
            let tooltip = endpoint.clone();
            rows = rows.child(
                div().flex().items_center().gap_3().py_4()
                    .border_t_1().border_color(cx.theme().border)
                    .child(
                        div().flex_1().min_w_0().flex().flex_col().gap_1()
                            .child(format!("UART {channel}"))
                            .child(
                                div().id(("overview-uart-path", channel as usize))
                                    .truncate().text_sm().text_color(cx.theme().muted_foreground)
                                    .tooltip(move |window, cx| Tooltip::new(tooltip.clone()).build(window, cx))
                                    .child(endpoint),
                            ),
                    )
                    .child(
                        Button::new(("overview-uart-copy", channel as usize))
                            .ghost().icon(Icon::empty().path("copy"))
                            .tooltip("复制连接路径")
                            .accessibility_label(format!("复制 UART {channel} 连接路径"))
                            .disabled(!ready)
                            .on_click(cx.listener(move |v, _, w, cx| {
                                v.act(Action::CopySerialPath(channel), w, cx)
                            })),
                    )
                    .child(
                        Switch::new(("overview-uart", channel as usize))
                            .label("启用").checked(ready)
                            .disabled(self.serial_busy(channel))
                            .on_change(cx.listener(move |v, _, w, cx| {
                                v.act(Action::SerialToggle(channel), w, cx)
                            })),
                    ),
            );
        }
        div().id("device-overview").flex_1().min_h_0().w_full().overflow_y_scroll()
            .p_6().flex().flex_col().items_center()
            .child(
                div().w_full().max_w(px(600.)).flex().flex_col().gap_5()
                    .child(
                        div().flex().items_center().justify_between().gap_3()
                            .child(
                                div().flex_1().min_w_0().flex().flex_col().gap_1()
                                    .child(div().text_lg().child(board.to_owned()))
                                    .child(caption(chip.to_owned())),
                            )
                            .child(caption(self.lifecycle())),
                    )
                    .child(rows),
            )
    }

    fn device_panel(&self, d: &Device, window: &Window, cx: &mut Context<Self>) -> Div {
        let on = self.powered();
        let indicators = d.hardware["board"]["indicators"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        let indicator_on = |id: &str| on && self.state.data["session"]["indicators"][id] == true;
        let pa_enabled = indicators.iter().any(|item| {
            item["kind"] == "audio-amplifier"
                && indicator_on(item["id"].as_str().unwrap_or_default())
        });
        let width = d.hardware["board"]["screen"]["width"]
            .as_f64()
            .unwrap_or(240.);
        let height = d.hardware["board"]["screen"]["height"]
            .as_f64()
            .unwrap_or(240.);
        let insets = gpui::component::window_paddings(window);
        let available = f32::from(window.viewport_size().width - insets.left - insets.right)
            - if self.signals_visible { 300. } else { 0. };
        let scale = (available - 64.).clamp(120., 336.) as f64 / width.max(height);
        let mut screen = div()
            .w(px((width * scale) as f32))
            .h(px((height * scale) as f32))
            .bg(rgb(0x07090b))
            .overflow_hidden();
        if on {
            if let Some(frame) = &self.state.frame {
                screen = screen.child(img(frame.clone()).size_full());
            }
        }
        if let Some(mapping) = touch::Mapping::from_descriptor(&d.hardware["board"]["touch"]).filter(|_| d.capabilities.touch) {
            let bounds = Rc::new(RefCell::new(None));
            let painted_bounds = bounds.clone();
            let move_bounds = bounds.clone();
            screen = screen
                .relative()
                .child(
                    gpui::canvas(
                        move |bounds, _, _| {
                            *painted_bounds.borrow_mut() = Some(bounds);
                        },
                        |_, _, _, _| {},
                    )
                    .absolute()
                    .top_0()
                    .left_0()
                    .size_full(),
                )
                .when(on, |e| e.cursor_pointer())
                .on_mouse_down(
                    MouseButton::Left,
                    cx.listener(move |v, event: &gpui::MouseDownEvent, _, _| {
                        if on {
                            if let Some(bounds) = *bounds.borrow() {
                                let (x, y) = mapping.position(bounds, event.position);
                                v.touch_at(x, y);
                            }
                        }
                    }),
                )
                .on_mouse_move(cx.listener(move |v, event: &gpui::MouseMoveEvent, _, _| {
                    if on && v.touch.is_some() {
                        if event.pressed_button == Some(MouseButton::Left) {
                            if let Some(bounds) = *move_bounds.borrow() {
                                let (x, y) = mapping.position(bounds, event.position);
                                v.touch_at(x, y);
                            }
                        } else {
                            v.release_touch();
                        }
                    }
                }))
                .on_mouse_up(
                    MouseButton::Left,
                    cx.listener(|v, _, _, _| v.release_touch()),
                )
                .on_mouse_up_out(
                    MouseButton::Left,
                    cx.listener(|v, _, _, _| v.release_touch()),
                );
        }
        let mut keys = div().flex().flex_wrap().justify_center().items_center().gap_3();
        for key in d.hardware["board"]["buttons"]
            .as_array()
            .cloned()
            .unwrap_or_default()
        {
            let id = key["id"].as_str().unwrap_or_default().to_owned();
            let down = id.clone();
            let up = id.clone();
            let out = id.clone();
            keys = keys.child(
                div()
                    .id(SharedString::from(format!("key-{id}")))
                    .px_6()
                    .py_3()
                    .rounded_lg()
                    .border_1()
                    .border_color(if self.held.contains(&id) {
                        cx.theme().primary
                    } else {
                        cx.theme().border
                    })
                    .bg(if self.held.contains(&id) {
                        cx.theme().accent
                    } else {
                        cx.theme().secondary
                    })
                    .when(!on, |e| e.opacity(0.3))
                    .when(on, |e| e.cursor_pointer())
                    .child(key["label"].as_str().unwrap_or_default().to_owned())
                    .on_mouse_down(
                        MouseButton::Left,
                        cx.listener(move |v, _, _, cx| {
                            if on {
                                v.button_state(&down, true);
                                cx.notify();
                            }
                        }),
                    )
                    .on_mouse_up(
                        MouseButton::Left,
                        cx.listener(move |v, _, _, cx| {
                            v.button_state(&up, false);
                            cx.notify();
                        }),
                    )
                    .on_mouse_up_out(
                        MouseButton::Left,
                        cx.listener(move |v, _, _, cx| {
                            v.button_state(&out, false);
                            cx.notify();
                        }),
                    ),
            );
        }
        let content = div()
            .relative()
            .flex_1()
            .min_w_0()
            .flex()
            .flex_col()
            .gap_4()
            .child(self.device_toolbar(d, &indicators, pa_enabled, window, cx))
            .child(
                div().flex_1().min_h_0().flex()
                    .child(
                        div().id("device-view").flex_1().min_w_0().flex().flex_col()
                            .overflow_y_scrollbar().items_center().justify_center().gap_5()
                            .when(d.capabilities.screen, |body| body.child(
                                div().p_4().rounded_2xl().bg(cx.theme().muted)
                                    .border_1().border_color(cx.theme().border)
                                    .child(screen),
                            ))
                            .when(d.hardware["board"]["buttons"].as_array().is_some_and(|buttons| !buttons.is_empty()),
                                |body| body.child(keys))
                            .when(!d.capabilities.screen
                                && d.hardware["board"]["buttons"].as_array().is_none_or(Vec::is_empty),
                                |body| body.child(self.device_overview(d, cx))),
                    )
                    .when(self.signals_visible, |body| {
                        body.child(
                            div().w(px(300.)).flex_shrink_0().min_h_0()
                                .flex().flex_col().p_4().gap_3()
                                .border_l_1().border_color(cx.theme().border)
                                .child(
                                    div().flex().items_center().justify_between()
                                        .child("信号输入")
                                        .child(self.control(
                                            "收起", "signals-close", Action::Signals,
                                            true, false, cx,
                                        )),
                                )
                                .child(self.signals.as_ref().unwrap().clone()),
                        )
                    }),
            );
        content
    }
}

fn application_menu() -> impl IntoElement {
    Button::new("application-menu")
        .icon(gpui::assets::IconName::Menu)
        .ghost()
        .w(px(32.))
        .h(px(32.))
        .accessibility_label("应用菜单")
        .tooltip("应用菜单")
        .dropdown_menu_with_anchor(gpui::Anchor::TopRight, |menu, _, _| {
            menu.item(PopupMenuItem::new("关于 Lisem").action(Box::new(ShowAbout)))
                .item(PopupMenuItem::new("安装命令行工具").action(Box::new(InstallCli)))
                .item(PopupMenuItem::new("退出 Lisem").action(Box::new(Quit)))
        })
}

#[derive(gpui::IntoElement)]
struct Caption(SharedString);

impl gpui::RenderOnce for Caption {
    fn render(self, _: &mut Window, cx: &mut App) -> impl IntoElement {
        div().text_xs().text_color(cx.theme().muted_foreground).child(self.0)
    }
}

fn caption(text: impl Into<SharedString>) -> Caption {
    Caption(text.into())
}
fn field(label: &str, input: Entity<InputState>) -> Div {
    div()
        .w_full()
        .flex()
        .flex_col()
        .gap_2()
        .child(caption(label.to_owned()))
        .child(Input::new(&input).aria_label(label.to_owned()))
}
impl Render for Desktop {
    fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        if self.creating {
            self.sync_board_options(window, cx);
        }
        if let Some(d) = self.device() {
            self.sync_camera_options(&d, window, cx);
        }
        let frame = if matches!(self.mode, Mode::Device(_))
            && !self.settings
            && !self.wifi_visible
            && self.powered()
        {
            self.state.frame.clone()
        } else {
            None
        };
        if !self
            .rendered_frame
            .as_ref()
            .zip(frame.as_ref())
            .is_some_and(|(old, new)| Arc::ptr_eq(old, new))
        {
            if let Some(old) = self.rendered_frame.take() {
                // Only this window painted the image. Retire it once, after
                // the replacement scene is rendered. Global eviction from
                // every polling view can invalidate another window's scene
                // and decrement GPUI's atlas reference count multiple times.
                window.on_next_frame(move |window, _| {
                    let _ = window.drop_image(old);
                });
            }
            self.rendered_frame = frame;
        }
        let title = match &self.mode {
            Mode::Library => "Lisem · 实例库".into(),
            Mode::Device(_) => self
                .device()
                .map(|d| format!("{} · Lisem", d.name))
                .unwrap_or_else(|| "Lisem · 设备".into()),
        };
        if self.title != title {
            window.set_window_title(&title);
            self.title = title;
        }
        if !window.is_window_active()
            || self.creating
            || self.settings
            || self.wifi_visible
        {
            self.release_buttons();
        }
        let mut layout = div().flex_1().min_h_0().flex();
        if self.creating {
            layout = layout.child(self.new_panel(cx));
        } else if matches!(self.mode, Mode::Library) {
            layout = layout.child(self.library_panel(cx));
        } else if let Some(d) = self.device() {
            if self.settings {
                layout = layout.child(self.settings_panel(&d, window, cx));
            } else if self.wifi_visible {
                layout = layout.child(self.wifi_panel());
            } else {
                layout = layout.child(self.device_panel(&d, window, cx));
            }
        } else {
            layout = layout.child(
                div()
                    .flex_1()
                    .flex()
                    .items_center()
                    .justify_center()
                    .child(caption("还没有实例")),
            );
        }
        let session = &self.state.data["session"];
        let error = self.state.error.as_deref().or_else(|| {
            if self.active() {
                session["error"]
                    .as_str()
                    .or(session["controls"]["error"].as_str())
            } else {
                None
            }
        });
        let message = if let Some(error) = error {
            error.to_owned()
        } else if !self.notice.is_empty() {
            self.notice.clone()
        } else {
            self.state.message.clone()
        };
        div()
            .size_full()
            .flex()
            .flex_col()
            .bg(cx.theme().background)
            .text_color(cx.theme().foreground)
            .text_base()
            .when(
                matches!(self.mode, Mode::Library)
                    || self.settings
                    || self.wifi_visible
                    || self.creating
                    || self.device().is_none(),
                |e| {
                    e.child(
                        TitleBar::new()
                            .on_close_window(cx.listener(|view, _, window, _| {
                                view.release_buttons();
                                window.remove_window();
                            }))
                            .h(px(52.))
                            .bg(cx.theme().background)
                            .when(
                                !cfg!(target_os = "macos")
                                    && matches!(self.mode, Mode::Library)
                                    && !self.creating,
                                |bar| {
                                    bar.child(
                                        div()
                                            .flex()
                                            .items_center()
                                            .gap_2()
                                            .pl_2()
                                            .flex_shrink_0()
                                            .child(img("app-icon").size(px(24.)))
                                            .child(div().text_base().child("Lisem")),
                                    )
                                },
                            )
                            .when(self.creating || self.settings || self.wifi_visible, |bar| {
                                let heading = if self.creating { "新建实例".to_owned() }
                                    else if self.wifi_visible { format!("{} · 热点", self.device().map(|d| d.name).unwrap_or_default()) }
                                    else { format!("{} · 设置", self.device().map(|d| d.name).unwrap_or_default()) };
                                bar.child(div().flex_1().min_w_0().px_4().flex().items_center().gap_3()
                                    .child(div().flex_1().min_w_0().truncate().text_lg().child(heading))
                                    .child(div().occlude().child(self.control(
                                        if self.creating { "取消" } else { "返回设备" },
                                        "page-back", Action::Cancel, true, false, cx))))
                            })
                            .when(
                                matches!(self.mode, Mode::Library) && !self.creating,
                                |bar| {
                                    bar.child(
                                        div()
                                            .flex_1()
                                            .flex()
                                            .justify_end()
                                            .items_center()
                                            .gap_1()
                                            .pr_6()
                                            .child(div().occlude().child(self.library_add_button(cx)))
                                            .when(!cfg!(target_os = "macos"), |row| {
                                                row.pr_4().child(div().occlude().child(application_menu()))
                                            }),
                                    )
                                },
                            ),
                    )
                },
            )
            .child(layout)
            .when(!message.is_empty(), |view| {
                view.child(
                    div()
                        .min_h(px(32.))
                        .px_5()
                        .py_2()
                        .border_t_1()
                        .border_color(cx.theme().border)
                        .text_xs()
                        .text_color(if error.is_some() { cx.theme().danger } else { cx.theme().muted_foreground })
                        .child(message),
                )
            })
    }
}

fn open_window(
    backend: Arc<Backend>,
    windows: WindowBook,
    mode: Mode,
    cx: &mut App,
) -> ManagedWindow {
    let library = matches!(mode, Mode::Library);
    let dimensions = if library {
        size(px(640.), px(480.))
    } else {
        size(px(800.), px(720.))
    };
    let (handle, view) = gpui::open_window(
        platform::window_options(WindowOptions {
            window_bounds: Some(WindowBounds::Windowed(Bounds::centered(
                None, dimensions, cx,
            ))),
            window_min_size: Some(if library {
                size(px(480.), px(360.))
            } else {
                size(px(700.), px(660.))
            }),
            titlebar: Some(TitlebarOptions {
                traffic_light_position: Some(gpui::point(px(16.), px(18.))),
                ..TitleBar::title_bar_options()
            }),
            app_id: Some("com.listenai.emulator".into()),
            app_owns_titlebar_drag: true,
            ..Default::default()
        }),
        cx,
        |window, cx| {
            let view = cx.new(|cx| Desktop::new(backend, windows, mode, window, cx));
            let weak = view.downgrade();
            window.on_window_should_close(cx, move |_, cx| {
                let _ = weak.update(cx, |v, _| v.release_buttons());
                true
            });
            view
        },
    )
    .expect("Could not open desktop window");
    ManagedWindow {
        handle,
        view: view.downgrade(),
    }
}
fn show_library(backend: Arc<Backend>, windows: WindowBook, cx: &mut App) -> ManagedWindow {
    let old = windows.borrow().library.clone();
    if let Some(handle) = old {
        if handle
            .handle
            .update(cx, |_, w, _| w.activate_window())
            .is_ok()
        {
            return handle;
        }
    }
    let handle = open_window(backend, windows.clone(), Mode::Library, cx);
    windows.borrow_mut().library = Some(handle.clone());
    cx.activate(true);
    handle
}
fn show_device(
    device: &Device,
    backend: Arc<Backend>,
    windows: WindowBook,
    cx: &mut App,
) -> ManagedWindow {
    let old = windows.borrow().devices.get(&device.id).cloned();
    if let Some(handle) = old {
        if handle
            .handle
            .update(cx, |_, w, _| w.activate_window())
            .is_ok()
        {
            return handle;
        }
    }
    let handle = open_window(
        backend,
        windows.clone(),
        Mode::Device(device.id.clone()),
        cx,
    );
    windows
        .borrow_mut()
        .devices
        .insert(device.id.clone(), handle.clone());
    cx.activate(true);
    handle
}
fn main() {
    lisem_core::process::init().expect("Cannot initialize process handles");
    struct Diagnostics;
    impl log::Log for Diagnostics {
        fn enabled(&self, m: &log::Metadata) -> bool {
            m.level() <= log::Level::Warn
        }
        fn log(&self, r: &log::Record) {
            if self.enabled(r.metadata()) {
                eprintln!("{}: {}", r.level(), r.args());
            }
        }
        fn flush(&self) {}
    }
    let _ = log::set_logger(&Diagnostics);
    log::set_max_level(log::LevelFilter::Warn);
    let root = lisem_core::paths::runtime_root(None).expect("Cannot locate runtime resources");
    let data = lisem_core::paths::data_dir().expect("Cannot resolve the instance library");
    let executable = std::env::var_os("LISEM_CLI")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            std::env::current_exe()
                .unwrap()
                .with_file_name(if cfg!(windows) { "lisem.exe" } else { "lisem" })
        });
    let backend = Backend::start(root, data, executable);
    let windows = Rc::new(RefCell::new(Windows::default()));
    let ui_backend = backend.clone();
    let reopen_backend = backend.clone();
    let reopen_windows = windows.clone();
    let application = gpui::application().with_assets(icons::Assets);
    application.on_reopen(move |cx| {
        show_library(reopen_backend.clone(), reopen_windows.clone(), cx);
    });
    application.run(move |cx: &mut App| {
        gpui::init(cx);
        theme::init(cx);
        cx.bind_keys([
            KeyBinding::new(
                if cfg!(target_os = "macos") {
                    "cmd-q"
                } else {
                    "ctrl-q"
                },
                Quit,
                None,
            ),
            KeyBinding::new(
                if cfg!(target_os = "macos") {
                    "cmd-0"
                } else {
                    "ctrl-0"
                },
                ShowLibrary,
                None,
            ),
            KeyBinding::new(
                if cfg!(target_os = "macos") {
                    "cmd-w"
                } else {
                    "ctrl-w"
                },
                CloseWindow,
                None,
            ),
        ]);
        cx.set_menus(vec![
            Menu {
                name: "Lisem".into(),
                disabled: false,
                items: vec![
                    MenuItem::action("关于 Lisem", ShowAbout),
                    MenuItem::separator(),
                    MenuItem::action("安装命令行工具", InstallCli),
                    MenuItem::separator(),
                    MenuItem::action("退出 Lisem", Quit),
                ],
            },
            Menu {
                name: "窗口".into(),
                disabled: false,
                items: vec![
                    MenuItem::action("实例库", ShowLibrary),
                    MenuItem::action("关闭窗口", CloseWindow),
                ],
            },
        ]);
        let b = ui_backend.clone();
        let w = windows.clone();
        cx.on_action(move |_: &ShowLibrary, cx| {
            let (b, w) = (b.clone(), w.clone());
            cx.defer(move |cx| {
                show_library(b, w, cx);
            });
        });
        let b = ui_backend.clone();
        let w = windows.clone();
        cx.on_action(move |_: &InstallCli, cx| {
            let (b, w) = (b.clone(), w.clone());
            cx.defer(move |cx| {
                let handle = show_library(b, w, cx);
                let _ = handle.view.update(cx, |view, cx| view.install_cli(cx));
            });
        });
        let w = windows.clone();
        cx.on_action(move |_: &ShowAbout, cx| {
            let w = w.clone();
            cx.defer(move |cx| {
                let mut book = w.borrow_mut();
                if let Some(handle) = book.about
                    && handle
                        .update(cx, |_, window, _| window.activate_window())
                        .is_ok()
                {
                    return;
                }
                book.about = Some(about::open(cx));
            });
        });
        cx.on_action(|_: &Quit, cx| cx.defer(|cx| cx.quit()));
        let w = windows.clone();
        cx.on_action(move |_: &CloseWindow, cx| {
            let active = cx
                .active_window()
                .or_else(|| cx.window_stack().and_then(|s| s.first().copied()));
            let w = w.clone();
            // The dispatching window is borrowed until this action returns.
            cx.defer(move |cx| {
                if let Some(active) = active {
                    let book = w.borrow();
                    for entry in book.library.iter().chain(book.devices.values()) {
                        if entry.handle == active {
                            let _ = entry.view.update(cx, |v, _| v.release_buttons());
                            break;
                        }
                    }
                    let _ = active.update(cx, |_, window, _| window.remove_window());
                }
            });
        });
        let shutdown = ui_backend.clone();
        let w = windows.clone();
        cx.on_app_quit(move |cx| {
            let book = w.borrow();
            for entry in book.library.iter().chain(book.devices.values()) {
                let _ = entry.view.update(cx, |v, _| v.release_buttons());
            }
            shutdown.request_shutdown();
            async {}
        })
        .detach();
        show_library(ui_backend, windows, cx);
    });
    backend.shutdown();
}
