#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

mod about;
mod backend;
mod display;
mod icons;
mod theme;

use backend::{Backend, Device, Snapshot};
use gpui::component::{
    ActiveTheme, Disableable, Icon, TitleBar,
    button::{Button, ButtonVariants},
    input::{Input, InputState},
    menu::{DropdownMenu, PopupMenuItem},
    switch::Switch,
    tab::{Tab, TabBar},
    tooltip::Tooltip,
};
use gpui::{
    AnyWindowHandle, App, Bounds, ClipboardItem, Context, Div, Entity, KeyBinding, Menu, MenuItem,
    MouseButton, PathPromptOptions, PromptLevel, SharedString, TitlebarOptions, WeakEntity, Window,
    WindowBounds, WindowOptions, actions, div, img, prelude::*, px, rgb, size,
};
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
    SaveName,
    Detach,
    Open,
    SerialMenu,
    AudioMenu,
    RegenerateUid,
    Reset,
    ResetDownload,
    ChoosePackage,
    ClearPackage,
    Create,
    Attach,
    Import,
    Erase,
    Start,
    Stop,
    Audio,
    Camera,
    ClearCamera,
    Sound,
    Network,
    Microphone,
    Serial(u8),
    SerialToggle(u8),
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
    settings_tab: &'static str,
    held: HashSet<String>,
    new_board: Option<String>,
    package: Option<PathBuf>,
    name: Entity<InputState>,
    title: String,
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
                        if let Some(id) = &view.selected {
                            state.data["session"] = state.data["sessions"][id].clone();
                            state.frame = state.frames.get(id).cloned();
                        }
                        let changed = view.state.data != state.data
                            || view.state.frame.as_ref().map(Arc::as_ptr)
                                != state.frame.as_ref().map(Arc::as_ptr)
                            || view.state.busy != state.busy
                            || view.state.error != state.error
                            || view.state.message != state.message
                            || view.state.operation != state.operation;
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
        Self {
            backend,
            windows,
            mode,
            state: Snapshot::default(),
            selected,
            notice: String::new(),
            creating: false,
            settings: false,
            settings_tab: "general",
            held: HashSet::new(),
            new_board: None,
            package: None,
            name: cx.new(|cx| InputState::new(window, cx).placeholder("实例名称")),
            title: String::new(),
            rendered_frame: None,
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
    fn send(&mut self, method: &'static str, params: Value) {
        self.notice.clear();
        self.backend.send(method, params);
        self.state.busy = true;
    }
    fn show_settings(&mut self, window: &mut Window, cx: &mut Context<Self>) {
        self.release_buttons();
        self.settings = true;
        self.settings_tab = "general";
        if let Some(d) = self.device() {
            self.name
                .update(cx, |name, cx| name.set_value(&d.name, window, cx));
        }
    }
    fn choose_file(&mut self, action: Action, cx: &mut Context<Self>) {
        let selected = self.selected.clone();
        let run = self.state.data["session"]["output"].clone();
        let directory = matches!(action, Action::Attach);
        let picker = cx.prompt_for_paths(PathPromptOptions {
            files: !directory,
            directories: directory,
            multiple: false,
            prompt: Some(
                match action {
                    Action::Attach => "打开实例目录",
                    Action::Audio => "选择 16 kHz 单声道 PCM16 WAV",
                    Action::Camera => "选择摄像头图片（PNG / JPEG / PNM）",
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
                            Action::Camera => {
                                v.send("camera", json!({"id":selected,"run":run,"path":path}))
                            }
                            Action::Audio => {
                                v.send("audio", json!({"id":selected,"run":run,"path":path}))
                            }
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
            }
            Action::Details | Action::SerialMenu | Action::AudioMenu => {}
            Action::Settings => self.show_settings(window, cx),
            Action::SaveName => self.send("rename", json!({
                "id": self.selected, "name": self.name.read(cx).value()
            })),
            Action::Open => {
                if let Some(d) = self.device() {
                    show_device(&d, self.backend.clone(), self.windows.clone(), cx);
                }
            }
            Action::ChoosePackage | Action::Attach | Action::Import | Action::Audio | Action::Camera => {
                self.choose_file(action, cx);
            }
            Action::ClearCamera => {
                let mut params = self.params();
                params["path"] = Value::Null;
                self.send("camera", params);
            }
            Action::ClearPackage => self.package = None,
            Action::Create => self.send("create", json!({
                "name": self.name.read(cx).value(), "board": self.new_board, "package": self.package
            })),
            Action::Start => self.send("start", json!({"id": self.selected})),
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
                    self.send("settings", json!({"id": d.id, "online": d.host["online"] != true}));
                }
            }
            Action::Microphone => {
                if let Some(d) = self.device() {
                    self.send("settings", json!({"id": d.id, "microphone": d.host["microphone"] != true}));
                }
            }
            Action::SerialToggle(channel) => {
                let enabled = self.serial_path(channel).is_none();
                self.send("serial", json!({"id": self.selected, "channel": channel, "enabled": enabled}));
            }
            Action::Serial(channel) => {
                if let Some(path) = self.serial_path(channel) {
                    let command = if let Some(address) = path.strip_prefix("tcp://") {
                        let (host, port) = address.rsplit_once(':').unwrap();
                        format!("putty -raw {host} -P {port}")
                    } else { format!("picocom {path}") };
                    cx.write_to_clipboard(ClipboardItem::new_string(command));
                    self.notice = "连接命令已复制".into();
                } else {
                    self.send("serial", json!({"id": self.selected, "channel": channel}));
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
                            "重新生成 UID？", "保留 Flash 和其余 OTP。依赖旧 UID 的平台登记不会自动转移。",
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
    fn release_buttons(&mut self) {
        for id in self.held.clone() {
            self.button_state(&id, false);
        }
    }
    fn serial_path(&self, channel: u8) -> Option<&str> {
        let identifier = self.selected.as_deref()?;
        self.state.data["serial"][identifier][channel.to_string()].as_str()
    }
    fn menu_items(&self, action: Action) -> Vec<(String, Action, bool, bool)> {
        let idle = !self.state.busy;
        let on = self.powered();
        match action {
            Action::SerialMenu => (0..3u8)
                .flat_map(|channel| {
                    let ready = self.serial_path(channel).is_some();
                    [
                        (
                            format!("UART {channel}"),
                            Action::SerialToggle(channel),
                            idle,
                            ready,
                        ),
                        (
                            format!("复制 UART {channel} 连接命令"),
                            Action::Serial(channel),
                            idle && ready,
                            false,
                        ),
                    ]
                })
                .collect(),
            Action::AudioMenu => vec![
                (
                    "声音输出".into(),
                    Action::Sound,
                    idle,
                    self.device().is_some_and(|d| d.host["sound"] == true),
                ),
                (
                    "麦克风".into(),
                    Action::Microphone,
                    idle && !on && self.state.data["capabilities"]["microphone"] == true,
                    self.device().is_some_and(|d| d.host["microphone"] == true),
                ),
                (
                    "输入 WAV…".into(),
                    Action::Audio,
                    idle && on
                        && self.device().is_some_and(|d| d.host["microphone"] != true)
                        && self.state.data["session"]["input_pending"] != true
                        && self.state.data["session"]["input_busy"] != true,
                    false,
                ),
            ],
            _ => vec![
                ("实例设置…".into(), Action::Settings, true, false),
                ("复制 UID".into(), Action::CopyUid, true, false),
                (
                    "复位到烧录模式".into(),
                    Action::ResetDownload,
                    idle && on
                        && self
                            .device()
                            .is_some_and(|d| d.hardware["board"]["id"] == "arcs-mini"),
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
            .ghost()
            .icon(Icon::empty().path(id))
            .accessibility_label(label)
            .tooltip(label)
            .disabled(!enabled)
            .when(lit, |b| b.text_color(cx.theme().primary))
            .when(
                (id == "power" || matches!(action, Action::AudioMenu)) && lit,
                |b| b.primary().text_color(cx.theme().primary_foreground),
            )
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
        if matches!(
            action,
            Action::Details | Action::AudioMenu | Action::SerialMenu
        ) {
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
    fn sidebar(&self, cx: &mut Context<Self>) -> Div {
        let mut list = div()
            .id("instances")
            .flex_1()
            .min_h_0()
            .overflow_y_scroll()
            .flex()
            .flex_col()
            .gap_2();
        for d in self.devices() {
            let id = d.id.clone();
            let selected = self.selected.as_ref() == Some(&id) && !self.creating;
            let on = self.state.data["sessions"][&id]["finished"] == false;
            list = list.child(
                div()
                    .id(SharedString::from(id.clone()))
                    .p_3()
                    .rounded_md()
                    .bg(rgb(if selected { 0x2a343f } else { 0x191d24 }))
                    .cursor_pointer()
                    .hover(|s| s.bg(cx.theme().secondary))
                    .child(div().text_sm().truncate().child(d.name))
                    .child(caption(format!(
                        "{}{}",
                        d.hardware["board"]["name"].as_str().unwrap_or("路径不可用"),
                        if on { " · ●" } else { "" }
                    )))
                    .on_click(cx.listener(move |v, ev: &gpui::ClickEvent, _, cx| {
                        v.release_buttons();
                        v.selected = Some(id.clone());
                        v.creating = false;
                        v.settings = false;
                        if ev.click_count() == 2 {
                            if let Some(d) = v.device() {
                                show_device(&d, v.backend.clone(), v.windows.clone(), cx);
                            }
                        }
                        cx.notify();
                    })),
            );
        }
        div()
            .w(px(232.))
            .flex_shrink_0()
            .h_full()
            .flex()
            .flex_col()
            .gap_4()
            .p_4()
            .bg(cx.theme().sidebar)
            .border_r_1()
            .border_color(cx.theme().border)
            .child(div().text_lg().py_2().child("实例库"))
            .child(list)
            .child(self.control(
                "＋ 新建实例",
                "new",
                Action::New,
                !self.state.busy,
                true,
                cx,
            ))
            .child(self.control(
                "打开实例…",
                "attach",
                Action::Attach,
                !self.state.busy,
                false,
                cx,
            ))
    }
    fn new_panel(&self, cx: &mut Context<Self>) -> Div {
        let mut form = div()
            .flex()
            .flex_col()
            .gap_5()
            .max_w(px(520.))
            .w_full()
            .child(div().text_xl().child("新建实例"))
            .child(field("名称", self.name.clone()));
        for board in self.state.data["boards"]
            .as_array()
            .cloned()
            .unwrap_or_default()
        {
            let id = board["id"].as_str().unwrap_or_default().to_owned();
            let selected = self.new_board.as_deref() == Some(&id);
            let name = board["name"].as_str().unwrap_or_default().to_owned();
            form = form.child(
                div()
                    .id(SharedString::from(format!("board-{id}")))
                    .p_4()
                    .rounded_lg()
                    .border_1()
                    .border_color(rgb(if selected { 0x65d4b4 } else { 0x38404b }))
                    .bg(cx.theme().secondary)
                    .cursor_pointer()
                    .child(name)
                    .child(caption(
                        board["chip"].as_str().unwrap_or_default().to_uppercase(),
                    ))
                    .on_click(cx.listener(move |v, _, _, cx| {
                        v.new_board = Some(id.clone());
                        cx.notify();
                    })),
            );
        }
        form = form
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
            )
            .child(
                div()
                    .flex()
                    .gap_3()
                    .child(self.control(
                        "创建实例",
                        "create",
                        Action::Create,
                        !self.state.busy && self.new_board.is_some(),
                        true,
                        cx,
                    ))
                    .child(self.control("取消", "cancel", Action::Cancel, true, false, cx)),
            );
        div()
            .flex_1()
            .flex()
            .items_center()
            .justify_center()
            .p_8()
            .child(form)
    }
    fn overview(&self, d: &Device, cx: &mut Context<Self>) -> Div {
        let mut v = div()
            .flex_1()
            .flex()
            .flex_col()
            .p_8()
            .gap_6()
            .child(div().text_2xl().child(d.name.clone()))
            .child(caption(self.lifecycle().to_owned()))
            .child(info(
                "板型",
                d.hardware["board"]["name"].as_str().unwrap_or("不可用"),
            ))
            .child(info(
                "芯片",
                d.hardware["chip"]["name"].as_str().unwrap_or("—"),
            ))
            .child(info("UID", &d.uid));
        if let Some(error) = &d.unavailable {
            v = v.child(caption(error.clone()));
        }
        v.child(
            div()
                .flex()
                .gap_3()
                .child(self.control(
                    "打开设备",
                    "open",
                    Action::Open,
                    d.unavailable.is_none(),
                    true,
                    cx,
                ))
                .child(self.control(
                    "设置",
                    "settings",
                    Action::Settings,
                    d.unavailable.is_none(),
                    false,
                    cx,
                )),
        )
        .child(div().flex_1())
        .child(
            div()
                .flex()
                .gap_3()
                .child(self.control("打开所在文件夹", "folder", Action::Folder, true, false, cx))
                .child(self.control(
                    "从列表移除",
                    "detach",
                    Action::Detach,
                    !self.powered() && !self.state.busy,
                    false,
                    cx,
                )),
        )
    }
    fn settings_panel(&self, d: &Device, cx: &mut Context<Self>) -> Div {
        let mutable = !self.powered() && !self.state.busy;
        let idle = !self.state.busy;
        let selected = match self.settings_tab {
            "storage" => 1,
            "connections" => 2,
            _ => 0,
        };
        let tabs = TabBar::new("settings-tabs")
            .segmented()
            .selected_index(selected)
            .child(Tab::new().label("概览"))
            .child(Tab::new().label("身份与存储"))
            .child(Tab::new().label("连接"))
            .on_click(cx.listener(|v, index: &usize, _, cx| {
                v.settings_tab = ["general", "storage", "connections"][*index];
                cx.notify();
            }));
        let mut body = div()
            .flex()
            .flex_col()
            .items_start()
            .gap_5()
            .max_w(px(600.))
            .w_full();
        match self.settings_tab {
            "storage" => {
                body = body
                    .child(info("芯片 UID", &d.uid))
                    .child(
                        div()
                            .flex()
                            .gap_3()
                            .child(self.control(
                                "复制 UID",
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
                    )
                    .child(info(
                        "Flash",
                        &format!(
                            "{} MiB",
                            d.hardware["board"]["flash_bytes"].as_u64().unwrap_or(0) / 1048576
                        ),
                    ))
                    .child(
                        div()
                            .flex()
                            .gap_3()
                            .child(self.control(
                                "从 LPK 写入 Flash…",
                                "import",
                                Action::Import,
                                mutable,
                                true,
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
                    )
                    .when(!mutable, |e| e.child(caption("下电后可修改身份和 Flash")));
            }
            "connections" => {
                body = body
                    .child(
                        Switch::new("network")
                            .label("宿主网络")
                            .checked(
                                d.host["online"] == true
                                    && self.state.data["capabilities"]["host_network"] == true,
                            )
                            .disabled(
                                !mutable || self.state.data["capabilities"]["host_network"] != true,
                            )
                            .on_change(cx.listener(|v, _, w, cx| v.act(Action::Network, w, cx))),
                    )
                    .child(caption("热点 Lisem，无密码；开启后通过电脑访问网络"))
                    .child(
                        Switch::new("output-sound")
                            .label("声音输出")
                            .checked(d.host["sound"] == true)
                            .disabled(!idle)
                            .on_change(cx.listener(|v, _, w, cx| v.act(Action::Sound, w, cx))),
                    )
                    .child(
                        Switch::new("microphone")
                            .label("麦克风")
                            .checked(d.host["microphone"] == true)
                            .disabled(
                                !mutable || self.state.data["capabilities"]["microphone"] != true,
                            )
                            .on_change(cx.listener(|v, _, w, cx| v.act(Action::Microphone, w, cx))),
                    )
                    .child(caption("下电后可切换；开启后连续录播"))
                    .child(self.control(
                        "输入 WAV…",
                        "audio",
                        Action::Audio,
                        idle && self.powered() && d.host["microphone"] != true,
                        false,
                        cx,
                    ));
                if d.hardware["board"]["camera"].is_object() {
                    let change = &self.state.data["session"]["camera_change"];
                    let pending = change["status"] == "pending";
                    let camera_notice = change["error"].as_str().unwrap_or_else(|| {
                        match change["status"].as_str() {
                            Some("pending") => "正在确认图片切换结果…",
                            Some("applied") => "图片输入已更新",
                            Some("unknown") => "未能确认图片切换结果",
                            _ => "保持比例，居中裁切至 640 × 480；重新上电时读取原文件",
                        }
                    });
                    body = body
                        .child(info(
                            "摄像头图片",
                            d.host["camera_image"]
                                .as_str()
                                .unwrap_or("未选择，采集等待输入"),
                        ))
                        .child(caption(camera_notice))
                        .child(
                            div()
                                .flex()
                                .gap_3()
                                .child(self.control(
                                    "选择图片…",
                                    "camera",
                                    Action::Camera,
                                    idle && !pending,
                                    false,
                                    cx,
                                ))
                                .child(self.control(
                                    "清除图片",
                                    "clear-camera",
                                    Action::ClearCamera,
                                    idle && !pending && d.host["camera_image"].is_string(),
                                    false,
                                    cx,
                                )),
                        );
                }
            }
            _ => {
                body = body
                    .child(field("名称", self.name.clone()))
                    .child(self.control("保存名称", "save-name", Action::SaveName, idle, false, cx))
                    .child(info(
                        "板型",
                        &format!(
                            "{} · v{}",
                            d.hardware["board"]["name"].as_str().unwrap_or(""),
                            d.hardware["board"]["version"].as_u64().unwrap_or(1)
                        ),
                    ))
                    .child(info(
                        "芯片",
                        &format!(
                            "{} · {}",
                            d.hardware["chip"]["name"].as_str().unwrap_or(""),
                            d.hardware["chip"]["family"]
                                .as_str()
                                .unwrap_or("")
                                .to_uppercase()
                        ),
                    ))
                    .child(info("实例位置", &d.path))
                    .child(self.control(
                        "打开所在文件夹",
                        "settings-folder",
                        Action::Folder,
                        true,
                        false,
                        cx,
                    ));
            }
        }
        div()
            .flex_1()
            .min_w_0()
            .flex()
            .flex_col()
            .gap_6()
            .p_6()
            .child(
                div()
                    .flex()
                    .justify_between()
                    .items_center()
                    .child(div().text_xl().child(format!("{} · 设置", d.name)))
                    .child(self.control("完成", "done", Action::Cancel, true, false, cx)),
            )
            .child(tabs)
            .child(
                div()
                    .id("settings-body")
                    .flex_1()
                    .min_h_0()
                    .overflow_y_scroll()
                    .child(body),
            )
    }
    fn device_panel(&self, d: &Device, cx: &mut Context<Self>) -> Div {
        let on = self.powered();
        let idle = !self.state.busy;
        let sound = d.host["sound"] == true;
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
        let scale = 336. / width.max(height);
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
        let mut keys = div().flex().items_center().gap_3();
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
                    .border_color(rgb(if self.held.contains(&id) {
                        0x65d4b4
                    } else {
                        0x38404b
                    }))
                    .bg(rgb(if self.held.contains(&id) {
                        0x254c41
                    } else {
                        0x242a33
                    }))
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
        let mut status_lights = div().flex().items_center().gap_1();
        for indicator in indicators
            .iter()
            .filter(|item| item["kind"] == "led" || item["kind"] == "signal")
        {
            let id = indicator["id"].as_str().unwrap_or_default();
            let label = indicator["label"].as_str().unwrap_or(id);
            let lit = indicator_on(id);
            let status = if !on {
                "已下电"
            } else {
                match self.state.data["session"]["indicators"][id].as_bool() {
                    Some(true) => "有效",
                    Some(false) => "无效",
                    None => "未驱动",
                }
            };
            let tooltip = format!("{label} · {status}");
            let color = indicator["color"]
                .as_str()
                .and_then(|color| u32::from_str_radix(color.trim_start_matches('#'), 16).ok())
                .unwrap_or(0x4cc3a2);
            status_lights = status_lights.child(
                div()
                    .id(SharedString::from(format!("indicator-{id}")))
                    .size(px(32.))
                    .flex_shrink_0()
                    .flex()
                    .items_center()
                    .justify_center()
                    .tooltip(move |window, cx| Tooltip::new(tooltip.clone()).build(window, cx))
                    .child(
                        div()
                            .size(px(10.))
                            .rounded_full()
                            .border_1()
                            .border_color(rgb(if lit { color } else { 0x48515e }))
                            .bg(rgb(if lit { color } else { 0x202630 })),
                    ),
            );
        }
        let content = div()
            .relative()
            .flex_1()
            .flex()
            .flex_col()
            .gap_4()
            .child(
                TitleBar::new()
                    .h(px(52.))
                    .bg(cx.theme().background)
                    .when(!cfg!(target_os = "macos"), |bar| {
                        bar.child(application_menu())
                    })
                    .child(
                        div()
                            .flex_1()
                            .min_w_0()
                            .pl_4()
                            .pr_4()
                            .flex()
                            .items_center()
                            .justify_between()
                            .gap_3()
                            .child(
                                div()
                                    .flex_1()
                                    .min_w_0()
                                    .truncate()
                                    .text_lg()
                                    .child(d.name.clone()),
                            )
                            .child(
                                div()
                                    // Controls must not inherit the title bar's native drag hitbox.
                                    .occlude()
                                    .flex_shrink_0()
                                    .flex()
                                    .items_center()
                                    .gap_1()
                                    .child(status_lights)
                                    .child(self.icon(
                                        if sound { "speaker" } else { "mute" },
                                        "声音",
                                        Action::AudioMenu,
                                        true,
                                        pa_enabled,
                                        cx,
                                    ))
                                    .child(self.icon(
                                        "serial",
                                        "串口",
                                        Action::SerialMenu,
                                        true,
                                        false,
                                        cx,
                                    ))
                                    .child(self.icon(
                                        "power",
                                        match self.lifecycle() {
                                            "已上电" => "下电",
                                            "已下电" => "上电",
                                            state => state,
                                        },
                                        if on { Action::Stop } else { Action::Start },
                                        idle && (!self.running() || on),
                                        on,
                                        cx,
                                    ))
                                    .child(self.icon(
                                        "reset",
                                        "复位",
                                        Action::Reset,
                                        idle && on,
                                        false,
                                        cx,
                                    ))
                                    .child(self.icon(
                                        "more",
                                        "实例",
                                        Action::Details,
                                        true,
                                        false,
                                        cx,
                                    )),
                            ),
                    ),
            )
            .child(
                div()
                    .flex_1()
                    .flex()
                    .flex_col()
                    .items_center()
                    .justify_center()
                    .gap_5()
                    .child(
                        div()
                            .p_4()
                            .rounded_2xl()
                            .bg(rgb(0x10141a))
                            .border_1()
                            .border_color(cx.theme().border)
                            .child(screen),
                    )
                    .child(keys),
            );
        content
    }
}

fn application_menu() -> impl IntoElement {
    Button::new("application-menu")
        .label("Lisem")
        .ghost()
        .dropdown_menu_with_anchor(gpui::Anchor::TopLeft, |menu, _, _| {
            menu.item(
                PopupMenuItem::new("关于 Lisem")
                    .on_click(|_, _, cx| cx.dispatch_action(&ShowAbout)),
            )
            .item(
                PopupMenuItem::new("安装命令行工具")
                    .on_click(|_, _, cx| cx.dispatch_action(&InstallCli)),
            )
            .item(PopupMenuItem::new("退出 Lisem").on_click(|_, _, cx| cx.dispatch_action(&Quit)))
        })
}

fn caption(text: impl Into<SharedString>) -> Div {
    div().text_xs().text_color(rgb(0x949eaf)).child(text.into())
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
fn info(label: &str, value: &str) -> Div {
    div()
        .flex()
        .flex_col()
        .gap_1()
        .child(caption(label.to_owned()))
        .child(div().text_sm().child(value.to_owned()))
}
impl Render for Desktop {
    fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let frame = if matches!(self.mode, Mode::Device(_)) && !self.settings && self.powered() {
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
        if !window.is_window_active() || self.creating || self.settings {
            self.release_buttons();
        }
        let mut layout = div().flex_1().min_h_0().flex();
        if matches!(self.mode, Mode::Library) {
            layout = layout.child(self.sidebar(cx));
        }
        if self.creating {
            layout = layout.child(self.new_panel(cx));
        } else if let Some(d) = self.device() {
            if self.settings {
                layout = layout.child(self.settings_panel(&d, cx));
            } else if matches!(self.mode, Mode::Library) {
                layout = layout.child(self.overview(&d, cx));
            } else {
                layout = layout.child(self.device_panel(&d, cx));
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
                    || self.creating
                    || self.device().is_none(),
                |e| {
                    e.child(
                        TitleBar::new()
                            .h(px(52.))
                            .bg(cx.theme().background)
                            .when(!cfg!(target_os = "macos"), |bar| {
                                bar.child(application_menu())
                            }),
                    )
                },
            )
            .child(layout)
            .child(
                div()
                    .min_h(px(32.))
                    .px_5()
                    .py_2()
                    .border_t_1()
                    .border_color(cx.theme().border)
                    .text_xs()
                    .text_color(rgb(if error.is_some() { 0xffa495 } else { 0xa5b2c3 }))
                    .child(message),
            )
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
        size(px(1000.), px(720.))
    } else {
        size(px(800.), px(720.))
    };
    let (handle, view) = gpui::open_window(
        WindowOptions {
            window_bounds: Some(WindowBounds::Windowed(Bounds::centered(
                None, dimensions, cx,
            ))),
            window_min_size: Some(if library {
                size(px(880.), px(640.))
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
        },
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
fn show_device(device: &Device, backend: Arc<Backend>, windows: WindowBook, cx: &mut App) {
    let old = windows.borrow().devices.get(&device.id).cloned();
    if let Some(handle) = old {
        if handle
            .handle
            .update(cx, |_, w, _| w.activate_window())
            .is_ok()
        {
            return;
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
        .insert(device.id.clone(), handle);
    cx.activate(true);
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
