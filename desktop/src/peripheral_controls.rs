//! Toolbar menus and settings share peripheral state and availability.
use crate::*;

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
            !self.state.busy
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
            "network" => "网络",
            _ => "串口",
        };
        let view = cx.entity().downgrade();
        Button::new(kind)
            .ghost()
            .icon(Icon::empty().path(kind))
            .accessibility_label(label)
            .tooltip(label)
            .when(active, |b| b.text_color(cx.theme().primary))
            .dropdown_menu_with_anchor(gpui::Anchor::TopRight, move |mut menu, window, cx| {
                let Some(view) = view.upgrade() else {
                    return menu;
                };
                if kind == "serial" {
                    for channel in 0..3u8 {
                        let owner = view.downgrade();
                        menu = menu.submenu(
                            format!("UART {channel}"),
                            window,
                            cx,
                            move |submenu, _, cx| {
                                let Some(owner) = owner.upgrade() else {
                                    return submenu;
                                };
                                owner.update(cx, |v, cx| {
                                    let ready = v.serial_path(channel).is_some();
                                    submenu
                                        .item(v.menu_action(
                                            "启用",
                                            Action::SerialToggle(channel),
                                            !v.state.busy,
                                            ready,
                                            cx,
                                        ))
                                        .item(v.menu_action(
                                            "复制连接命令",
                                            Action::Serial(channel),
                                            !v.state.busy && ready,
                                            false,
                                            cx,
                                        ))
                                })
                            },
                        );
                    }
                    menu
                } else {
                    view.update(cx, |v, cx| v.populate_peripheral_menu(kind, menu, cx))
                }
            })
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
                    .item(self.audio_choice("系统麦克风", true, true, &d, cx))
                    .separator()
                    .item(self.menu_action(
                        "输入 WAV…",
                        Action::Audio,
                        self.wav_input_enabled(&d),
                        false,
                        cx,
                    ));
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
                    "允许联网",
                    Action::Network,
                    self.network_change_enabled(),
                    d.host["online"] == true,
                    cx,
                ));
                if self.running() {
                    menu = menu.separator().label("下电后可更改");
                }
                menu
            }
            _ => menu,
        }
    }

    pub(super) fn camera_change_enabled(&self) -> bool {
        let pending = &self.state.data["session"]["camera_change"];
        !self.state.busy
            && (pending["status"] != "pending" || pending["phase"] == "waiting-first-frame")
    }

    pub(super) fn network_change_enabled(&self) -> bool {
        !self.running()
            && !self.state.busy
            && self.state.data["capabilities"]["host_network"] == true
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
            .is_some_and(|(op, _)| op == "microphone")
    }

    pub(super) fn microphone_change_enabled(&self) -> bool {
        !self.state.busy
            && !self.microphone_pending()
            && self.state.data["session"]["input_busy"] != true
    }

    pub(super) fn wav_input_enabled(&self, d: &Device) -> bool {
        self.microphone_change_enabled() && self.powered() && !self.host_microphone_enabled(d)
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
            "正在输入 WAV，结束后可切换麦克风".into()
        } else if self.host_microphone_enabled(d) && !self.powered() {
            "已启用，上电后采集系统麦克风".into()
        } else if self.host_microphone_enabled(d) {
            "使用电脑的麦克风；关闭后可输入 WAV".into()
        } else if !self.powered() {
            "上电后可输入 WAV".into()
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
                            .label("允许联网")
                            .checked(d.host["online"] == true)
                            .disabled(!self.network_change_enabled())
                            .on_change(cx.listener(|v, _, w, cx| v.act(Action::Network, w, cx))),
                    )
                    .child(caption(if self.running() {
                        "下电后可更改；下次上电生效"
                    } else {
                        "开启后，固件可通过热点 Lisem 访问网络"
                    })),
            ))
            .child(settings_row(
                "扬声器",
                Switch::new("settings-speaker")
                    .label("系统扬声器")
                    .checked(d.host["sound"] == true)
                    .disabled(self.state.busy)
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
                    .child(caption(self.microphone_notice(d)))
                    .child(self.control(
                        "输入 WAV…",
                        "settings-wav",
                        Action::Audio,
                        self.wav_input_enabled(d),
                        false,
                        cx,
                    )),
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
                            .disabled(self.state.busy)
                            .on_change(cx.listener(move |v, _, w, cx| {
                                v.act(Action::SerialToggle(channel), w, cx)
                            })),
                    )
                    .child(
                        Button::new(("settings-uart-copy", channel as usize))
                            .ghost()
                            .icon(Icon::empty().path("copy"))
                            .tooltip("复制连接命令")
                            .accessibility_label(format!("复制 UART {channel} 连接命令"))
                            .disabled(!ready || self.state.busy)
                            .on_click(cx.listener(move |v, _, w, cx| {
                                v.act(Action::Serial(channel), w, cx)
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
