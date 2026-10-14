//! Toolbar menus and settings share peripheral state and availability.
use crate::*;
use gpui::{assets::IconName, component::WindowExt};

impl Desktop {
    pub(super) fn sync_camera_options(
        &mut self,
        d: &Device,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) {
        let (options, selected) = camera_selector::options(
            &d.host,
            &self.state.cameras,
            self.state.data["capabilities"]["host_camera"] == true,
        );
        let options_changed = self.camera_options != options;
        if options_changed || self.camera_select.read(cx).selected_value() != Some(&selected) {
            self.camera_select.update(cx, |select, cx| {
                if options_changed {
                    select.set_items(options.clone(), window, cx);
                }
                select.set_selected_value(&selected, window, cx);
            });
            self.camera_options = options;
        }
    }

    pub(super) fn camera_notice(&self, d: &Device) -> String {
        let change = &self.state.data["session"]["camera_change"];
        let pending = change["status"] == "pending";
        let waiting_first_frame = pending && change["phase"] == "waiting-first-frame";
        let host_camera = self.state.data["capabilities"]["host_camera"] == true;
        let selected_device = d.host["camera_device"].as_str();
        let capture = &self.state.data["session"]["camera_capture"];
        let camera_notice = if let Some(error) = change["error"].as_str() {
            error.to_owned()
        } else if waiting_first_frame {
            "正在打开摄像头，等待授权或第一帧；可关闭输入取消".into()
        } else if pending {
            "正在确认输入切换结果…".into()
        } else if change["status"] == "unknown" {
            "未能确认输入切换结果，请检查设备状态".into()
        } else if selected_device.is_some() && self.powered() {
            match capture["state"].as_str() {
                Some("starting") => "正在打开摄像头，等待第一帧…".into(),
                Some("live") => "宿主摄像头采集中".into(),
                Some("disconnected") => "摄像头已断开，请重新连接后选择".into(),
                Some("error") => capture["error"]
                    .as_str()
                    .unwrap_or("摄像头无法采集，请检查系统权限或重新选择")
                    .into(),
                _ => "等待摄像头输入".into(),
            }
        } else if selected_device.is_some() {
            "已选择，上电后开启摄像头".into()
        } else if d.host["camera_image"].is_string() {
            "保持比例，居中裁切；重新上电时读取原文件".into()
        } else if host_camera {
            "选择图片或宿主摄像头作为场景输入".into()
        } else {
            "选择图片作为场景输入".into()
        };
        camera_notice
    }

    pub(super) fn choose_camera(
        &mut self,
        choice: &CameraChoice,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) {
        match choice {
            CameraChoice::Off => self.act(Action::ClearCamera, window, cx),
            CameraChoice::Image => self.act(Action::Camera, window, cx),
            CameraChoice::Refresh => self.act(Action::RefreshCameras, window, cx),
            CameraChoice::Device(id) => {
                let mut params = self.params();
                params["device_id"] = json!(id);
                self.send("camera", params);
            }
        }
    }

    fn menu_action(
        &self,
        label: impl Into<SharedString>,
        action: Action,
        enabled: bool,
        checked: bool,
        cx: &Context<Self>,
    ) -> PopupMenuItem {
        let view = cx.entity().downgrade();
        PopupMenuItem::new(label)
            .disabled(!enabled)
            .checked(checked)
            .on_click(move |_, window, cx| {
                let _ = view.update(cx, |v, cx| v.act(action, window, cx));
            })
    }

    fn audio_choice(
        &self,
        label: &'static str,
        microphone: bool,
        selected: bool,
        d: &Device,
        cx: &Context<Self>,
    ) -> PopupMenuItem {
        let current = if microphone {
            self.host_microphone_enabled(d)
        } else {
            d.host["sound"] == true
        };
        let available = if microphone {
            self.microphone_change_enabled()
        } else {
            !self.control_busy(&["sound"])
        };
        let view = cx.entity().downgrade();
        PopupMenuItem::new(label)
            .checked(current == selected)
            .disabled(!available)
            .on_click(move |_, _, cx| {
                let _ = view.update(cx, |v, _| {
                    let Some(d) = v.device() else {
                        return;
                    };
                    let mut params = if microphone && v.powered() {
                        v.params()
                    } else {
                        json!({"id":d.id})
                    };
                    params["enabled"] = json!(selected);
                    v.send(if microphone { "microphone" } else { "sound" }, params);
                });
            })
    }

    pub(super) fn peripheral_menu(
        &self,
        kind: &'static str,
        active: bool,
        cx: &mut Context<Self>,
    ) -> impl IntoElement {
        let label = match kind {
            "camera" => "摄像头",
            "microphone" => "麦克风",
            "speaker" => "扬声器",
            "network" => "Wi-Fi",
            "sd" => "SD 卡",
            "usb" => "USB",
            _ => "串口",
        };
        let disabled = self.device().is_some_and(|d| match kind {
            "camera" => !d.host["camera_device"].is_string() && !d.host["camera_image"].is_string(),
            "microphone" => !self.host_microphone_enabled(&d),
            "speaker" => d.host["sound"] != true,
            "network" => !self.access_point_present(&d),
            _ => false,
        });
        let icon = match (kind, disabled) {
            ("camera", true) => Icon::new(IconName::CameraOff),
            ("camera", false) => Icon::new(IconName::Camera),
            ("microphone", true) => Icon::new(IconName::MicOff),
            ("microphone", false) => Icon::new(IconName::Mic),
            ("speaker", true) => Icon::new(IconName::VolumeOff),
            ("speaker", false) => Icon::new(IconName::Volume2),
            ("sd", _) => Icon::new(IconName::CardSim),
            ("usb", _) => Icon::new(IconName::Usb),
            ("network", true) => Icon::new(IconName::WifiOff),
            ("network", false) => Icon::new(IconName::Wifi),
            _ => Icon::empty().path(kind),
        };
        let view = cx.entity().downgrade();
        Button::new(kind)
            .ghost()
            .icon(icon)
            .accessibility_label(label)
            .tooltip(label)
            .when(active && !disabled, |b| b.text_color(cx.theme().primary))
            .dropdown_menu_with_anchor(gpui::Anchor::TopRight, move |menu, _, cx| {
                let Some(view) = view.upgrade() else {
                    return menu;
                };
                view.update(cx, |v, cx| v.populate_peripheral_menu(kind, menu, cx))
            })
    }

    pub(super) fn show_serial_info(&self, window: &mut Window, cx: &mut Context<Self>) {
        let owner = cx.entity().downgrade();
        window.open_dialog(cx, move |dialog, _, cx| {
            let dialog = dialog.title("串口连接").width(px(480.));
            let Some(owner) = owner.upgrade() else {
                return dialog;
            };
            let view = owner.read(cx);
            let mut content = div().flex().flex_col().gap_3();
            let mut count = 0;
            for channel in 0..3u8 {
                let Some(path) = view.serial_path(channel) else {
                    continue;
                };
                count += 1;
                let weak = owner.downgrade();
                content = content.child(
                    div()
                        .flex()
                        .items_center()
                        .gap_3()
                        .child(
                            div()
                                .flex_1()
                                .min_w_0()
                                .child(format!("UART {channel}"))
                                .child(caption(path.to_owned())),
                        )
                        .child(
                            Button::new(SharedString::from(format!("copy-uart-{channel}")))
                                .ghost()
                                .icon(IconName::Copy)
                                .label("复制路径")
                                .on_click(move |_, window, cx| {
                                    let _ = weak.update(cx, |v, cx| {
                                        v.act(Action::CopySerialPath(channel), window, cx)
                                    });
                                }),
                        ),
                );
            }
            if count == 0 {
                content = content.child(caption("尚未启用串口"));
            }
            dialog.child(content)
        });
    }

    fn populate_peripheral_menu(
        &self,
        kind: &str,
        mut menu: PopupMenu,
        cx: &mut Context<Self>,
    ) -> PopupMenu {
        let Some(d) = self.device() else {
            return menu;
        };
        match kind {
            "usb" => {
                menu = menu.item(self.menu_action(
                    "ADB 桥接", Action::UsbAdb, self.usb_change_enabled(), self.usb_enabled(), cx,
                ));
                if let Some(address) = self.usb_address() {
                    menu = menu.item(self.menu_action(
                        address.to_owned(), Action::CopyUsbAddress, true, false, cx,
                    ));
                }
                menu.separator().label(self.usb_notice())
            }
            "sd" => {
                menu = menu
                    .label(self.sd_label(&d))
                    .separator()
                    .item(self.menu_action(
                        "插入镜像…",
                        Action::MountSd,
                        self.sd_change_enabled(),
                        false,
                        cx,
                    ))
                    .item(self.menu_action(
                        "弹出",
                        Action::EjectSd,
                        self.sd_change_enabled() && self.sd_medium(&d)["path"].is_string(),
                        false,
                        cx,
                    ));
                if let Some(notice) = self.sd_notice() {
                    menu = menu.separator().label(notice);
                }
                menu
            }
            "serial" => {
                for channel in 0..3u8 {
                    menu = menu.item(self.menu_action(
                        format!("UART {channel}"),
                        Action::SerialToggle(channel),
                        !self.serial_busy(channel),
                        self.serial_path(channel).is_some(),
                        cx,
                    ));
                }
                menu.separator().item(self.menu_action(
                    "连接信息…",
                    Action::SerialInfo,
                    true,
                    false,
                    cx,
                ))
            }
            "camera" => {
                let (options, selected) = camera_selector::options(
                    &d.host,
                    &self.state.cameras,
                    self.state.data["capabilities"]["host_camera"] == true,
                );
                for option in options {
                    let choice = option.value().clone();
                    if choice == CameraChoice::Refresh {
                        menu = menu.separator();
                    }
                    let view = cx.entity().downgrade();
                    menu = menu.item(
                        PopupMenuItem::new(option.title())
                            .checked(choice == selected)
                            .disabled(!self.camera_change_enabled())
                            .on_click(move |_, window, cx| {
                                let _ =
                                    view.update(cx, |v, cx| v.choose_camera(&choice, window, cx));
                            }),
                    );
                }
                let change = &self.state.data["session"]["camera_change"];
                let capture = &self.state.data["session"]["camera_capture"];
                let needs_notice = matches!(change["status"].as_str(), Some("pending" | "unknown"))
                    || change["error"].is_string()
                    || (self.powered()
                        && d.host["camera_device"].is_string()
                        && matches!(
                            capture["state"].as_str(),
                            Some("starting" | "disconnected" | "error")
                        ));
                if needs_notice {
                    menu.separator().label(self.camera_notice(&d))
                } else {
                    menu
                }
            }
            "microphone" => {
                menu = menu
                    .item(self.audio_choice("关闭", true, false, &d, cx))
                    .item(self.audio_choice("系统麦克风", true, true, &d, cx));
                let change = &self.state.data["session"]["microphone_change"];
                if self.microphone_pending()
                    || change["error"].is_string()
                    || self.state.data["session"]["input_busy"] == true
                {
                    menu.separator().label(self.microphone_notice(&d))
                } else {
                    menu
                }
            }
            "speaker" => menu
                .item(self.audio_choice("关闭", false, false, &d, cx))
                .item(self.audio_choice("系统扬声器", false, true, &d, cx)),
            "network" => {
                menu = menu.item(self.menu_action(
                    "热点可用",
                    Action::Network,
                    self.network_change_enabled(),
                    self.access_point_present(&d),
                    cx,
                ));
                menu = menu.separator().item(self.menu_action(
                    "热点设置…",
                    Action::WifiSettings,
                    true,
                    false,
                    cx,
                ));
                if let Some(notice) = self.access_point_notice() {
                    menu = menu.separator().label(notice);
                }
                menu
            }
            _ => menu,
        }
    }

    pub(super) fn usb_enabled(&self) -> bool {
        self.powered() && self.state.data["session"]["usb_adb"]["enabled"] == true
    }

    pub(super) fn usb_configured(&self) -> bool {
        self.usb_enabled() && self.state.data["session"]["usb_adb"]["connected"] == true
            && self.state.data["session"]["usb_adb"]["status"] == "configured"
    }

    pub(super) fn usb_change_enabled(&self) -> bool {
        self.powered() && self.state.data["session"]["usb_adb"].is_object()
            && !self.control_busy(&["usb_adb"])
    }

    pub(super) fn usb_address(&self) -> Option<&str> {
        self.usb_enabled().then(|| self.state.data["session"]["usb_adb"]["listen"].as_str()).flatten()
    }

    pub(super) fn usb_notice(&self) -> String {
        if !self.powered() { return "上电后可开启".into(); }
        if !self.state.data["session"]["usb_adb"].is_object() {
            return "当前运行不支持 ADB 桥接".into();
        }
        if self.control_busy(&["usb_adb"]) { return "正在切换…".into(); }
        if !self.usb_enabled() { return "已关闭".into(); }
        let state = &self.state.data["session"]["usb_adb"];
        if let Some(error) = state["error"].as_str() { return format!("连接异常：{error}"); }
        if self.usb_configured() { return "已连接".into(); }
        if state["connected"] != true { return "等待连接".into(); }
        match state["status"].as_str() {
            Some("connected") => "等待固件 USB 就绪".into(),
            _ => "正在枚举…".into(),
        }
    }

    pub(super) fn sd_medium(&self, d: &Device) -> Value {
        if self.powered() {
            self.state.data["session"]["sd_card"].clone()
        } else {
            d.media["sd"].clone()
        }
    }

    fn sd_label(&self, d: &Device) -> String {
        let medium = self.sd_medium(d);
        medium["path"]
            .as_str()
            .map(|path| {
                std::path::Path::new(path)
                    .file_name()
                    .map(|name| name.to_string_lossy().into_owned())
                    .unwrap_or_else(|| path.to_owned())
            })
            .unwrap_or_else(|| "未插卡".into())
    }

    fn sd_change_enabled(&self) -> bool {
        !self.control_busy(&["sd_card"])
            && self.state.data["session"]["sd_change"]["status"] != "pending"
    }

    fn sd_notice(&self) -> Option<String> {
        let change = &self.state.data["session"]["sd_change"];
        if change["status"] == "pending" {
            Some("正在切换 SD 卡…".into())
        } else {
            change["error"].as_str().map(str::to_owned)
        }
    }

    pub(super) fn camera_change_enabled(&self) -> bool {
        let pending = &self.state.data["session"]["camera_change"];
        !self.control_busy(&["camera", "camera_host"])
            && (pending["status"] != "pending" || pending["phase"] == "waiting-first-frame")
    }

    pub(super) fn access_point_present(&self, d: &Device) -> bool {
        if self.powered() {
            self.state.data["session"]["wifi_ap_present"]
                .as_bool()
                .unwrap_or(d.host["wifi_ap_present"] != false)
        } else {
            d.host["wifi_ap_present"] != false
        }
    }

    fn access_point_notice(&self) -> Option<String> {
        let change = &self.state.data["session"]["access_point_change"];
        if change["status"] == "pending" {
            Some("正在切换热点…".into())
        } else {
            change["error"].as_str().map(str::to_owned)
        }
    }

    pub(super) fn network_change_enabled(&self) -> bool {
        !self.control_busy(&["access_point"])
            && self.state.data["session"]["access_point_change"]["status"] != "pending"
    }

    pub(super) fn host_microphone_enabled(&self, d: &Device) -> bool {
        if self.powered() {
            self.state.data["session"]["microphone"]
                .as_bool()
                .unwrap_or(d.host["microphone"] == true)
        } else {
            d.host["microphone"] == true
        }
    }

    fn microphone_pending(&self) -> bool {
        matches!(
            self.state.data["session"]["microphone_change"]["status"].as_str(),
            Some("pending" | "unknown")
        ) || self
            .state
            .operation
            .as_ref()
            .is_some_and(|(op, params)| op == "microphone"
                && params["id"].as_str() == self.selected.as_deref())
    }

    pub(super) fn microphone_change_enabled(&self) -> bool {
        !self.control_busy(&["microphone"])
            && !self.microphone_pending()
            && self.state.data["session"]["input_busy"] != true
    }

    pub(super) fn microphone_notice(&self, d: &Device) -> String {
        let change = &self.state.data["session"]["microphone_change"];
        if change["status"] == "unknown" {
            "仍在等待宿主确认，暂时不能再次切换".into()
        } else if let Some(error) = change["error"].as_str() {
            error.into()
        } else if self.microphone_pending() {
            "正在切换麦克风…".into()
        } else if self.state.data["session"]["input_busy"] == true {
            "正在输入音频，结束后可切换麦克风".into()
        } else if self.host_microphone_enabled(d) && !self.powered() {
            "已启用，上电后采集系统麦克风".into()
        } else if self.host_microphone_enabled(d) {
            "正在采集系统麦克风".into()
        } else {
            "未采集系统麦克风".into()
        }
    }

    pub(super) fn media_settings(&self, d: &Device, cx: &mut Context<Self>) -> Div {
        let mut body = div()
            .flex()
            .flex_col()
            .gap_3()
            .child(settings_row(
                "网络",
                div()
                    .flex()
                    .flex_col()
                    .gap_2()
                    .child(
                        Switch::new("settings-network")
                            .label("热点可用")
                            .checked(self.access_point_present(&d))
                            .disabled(!self.network_change_enabled())
                            .on_change(cx.listener(|v, _, w, cx| v.act(Action::Network, w, cx))),
                    )
                    .when_some(self.access_point_notice(), |view, notice| {
                        view.child(caption(notice))
                    }),
            ))
            .child(settings_row(
                "扬声器",
                Switch::new("settings-speaker")
                    .label("系统扬声器")
                    .checked(d.host["sound"] == true)
                    .disabled(self.control_busy(&["sound"]))
                    .on_change(cx.listener(|v, _, w, cx| v.act(Action::Sound, w, cx))),
            ))
            .child(settings_row(
                "麦克风",
                div()
                    .flex()
                    .flex_col()
                    .items_start()
                    .gap_2()
                    .child(
                        Switch::new("settings-microphone")
                            .label("系统麦克风")
                            .checked(self.host_microphone_enabled(d))
                            .disabled(!self.microphone_change_enabled())
                            .on_change(cx.listener(|v, _, w, cx| v.act(Action::Microphone, w, cx))),
                    )
                    .child(caption(self.microphone_notice(d))),
            ))
            .child(settings_row(
                "摄像头",
                div()
                    .flex()
                    .flex_col()
                    .gap_2()
                    .child(
                        Select::new(&self.camera_select)
                            .id("settings-camera")
                            .accessibility_label("摄像头输入")
                            .w_full()
                            .disabled(!self.camera_change_enabled()),
                    )
                    .child(caption(self.camera_notice(d))),
            ));
        body = body.child(settings_row(
            "USB",
            div().flex().flex_col().items_start().gap_2()
                .child(Switch::new("settings-usb-adb")
                    .label("ADB 桥接")
                    .checked(self.usb_enabled())
                    .disabled(!self.usb_change_enabled())
                    .on_change(cx.listener(|v, _, w, cx| v.act(Action::UsbAdb, w, cx))))
                .when_some(self.usb_address(), |view, address| view.child(
                    Button::new("settings-usb-copy").ghost().icon(IconName::Copy)
                        .label(address.to_owned()).tooltip("复制地址")
                        .on_click(cx.listener(|v, _, w, cx| v.act(Action::CopyUsbAddress, w, cx)))))
                .child(caption(self.usb_notice())),
        ));
        body = body.child(settings_row(
            "SD 卡",
            div()
                .flex()
                .flex_col()
                .gap_2()
                .child(div().truncate().child(self.sd_label(d)))
                .child(
                    div()
                        .flex()
                        .gap_2()
                        .child(
                            Button::new("settings-sd-mount")
                                .label("插入镜像…")
                                .disabled(!self.sd_change_enabled())
                                .on_click(cx.listener(|v, _, w, cx| v.act(Action::MountSd, w, cx))),
                        )
                        .child(
                            Button::new("settings-sd-eject")
                                .label("弹出")
                                .disabled(
                                    !self.sd_change_enabled()
                                        || !self.sd_medium(d)["path"].is_string(),
                                )
                                .on_click(cx.listener(|v, _, w, cx| v.act(Action::EjectSd, w, cx))),
                        ),
                )
                .when_some(self.sd_notice(), |view, notice| view.child(caption(notice))),
        ));
        for channel in 0..3u8 {
            let ready = self.serial_path(channel).is_some();
            body = body.child(settings_row(
                &format!("UART {channel}"),
                div()
                    .flex()
                    .items_center()
                    .gap_3()
                    .child(
                        Switch::new(("settings-uart", channel as usize))
                            .label("启用")
                            .checked(ready)
                            .disabled(self.serial_busy(channel))
                            .on_change(cx.listener(move |v, _, w, cx| {
                                v.act(Action::SerialToggle(channel), w, cx)
                            })),
                    )
                    .child(
                        Button::new(("settings-uart-copy", channel as usize))
                            .ghost()
                            .icon(Icon::empty().path("copy"))
                            .tooltip("复制连接路径")
                            .accessibility_label(format!("复制 UART {channel} 连接路径"))
                            .disabled(!ready)
                            .on_click(cx.listener(move |v, _, w, cx| {
                                v.act(Action::CopySerialPath(channel), w, cx)
                            })),
                    ),
            ));
        }
        body
    }
}

pub(crate) fn settings_row(label: &str, content: impl IntoElement) -> Div {
    div()
        .w_full()
        .flex()
        .items_start()
        .gap_4()
        .py_3()
        .child(
            div()
                .w(px(100.))
                .flex_shrink_0()
                .text_sm()
                .child(label.to_owned()),
        )
        .child(div().flex_1().min_w_0().child(content))
}
