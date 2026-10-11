//! Instance launcher and device-local navigation.
use crate::*;
use gpui::component::menu::ContextMenuExt;

impl Desktop {
    fn select_instance(&mut self, id: &str) {
        self.selected = Some(id.to_owned());
        self.state.data["session"] = self.state.data["sessions"][id].clone();
        self.state.frame = self.state.frames.get(id).cloned();
    }

    fn instance_menu(&self, mut menu: PopupMenu, device: &Device, cx: &Context<Self>) -> PopupMenu {
        for (label, action, enabled) in [
            ("打开设备", Action::Open, device.unavailable.is_none()),
            ("设置…", Action::Settings, device.unavailable.is_none()),
            ("打开所在文件夹", Action::Folder, true),
            (
                "从列表移除…",
                Action::Detach,
                !self.state.busy && self.state.data["sessions"][&device.id]["finished"] != false,
            ),
        ] {
            let id = device.id.clone();
            let view = cx.entity().downgrade();
            menu = menu.item(PopupMenuItem::new(label).disabled(!enabled).on_click(
                move |_, window, cx| {
                    let _ = view.update(cx, |view, cx| {
                        view.select_instance(&id);
                        view.act(action, window, cx);
                    });
                },
            ));
        }
        menu
    }

    pub(super) fn library_add_button(&self, cx: &mut Context<Self>) -> gpui::AnyElement {
        let weak = cx.entity().downgrade();
        Button::new("add-instance")
            .ghost()
            .icon(gpui::assets::IconName::Plus)
            .accessibility_label("添加实例")
            .tooltip("添加实例")
            .disabled(self.state.busy)
            .dropdown_menu_with_anchor(gpui::Anchor::TopRight, move |mut menu, _, _| {
                for (label, action) in [
                    ("新建实例…", Action::New),
                    ("打开已有实例…", Action::Attach),
                ] {
                    let weak = weak.clone();
                    menu = menu.item(PopupMenuItem::new(label).on_click(move |_, window, cx| {
                        let _ = weak.update(cx, |view, cx| view.act(action, window, cx));
                    }));
                }
                menu
            })
            .into_any_element()
    }

    pub(super) fn library_panel(&self, cx: &mut Context<Self>) -> Div {
        let devices = self.devices();
        let mut list = div().id("instances").flex_1().min_h_0().overflow_y_scroll();
        if devices.is_empty() {
            list = list.flex().items_center().justify_center().child(caption(
                if self.state.data["devices"].is_array() {
                    "还没有实例"
                } else if self.state.error.is_some() {
                    "无法加载实例库"
                } else {
                    "正在加载实例…"
                },
            ));
        }
        for device in devices {
            let id = device.id.clone();
            let settings_id = id.clone();
            let session = &self.state.data["sessions"][&id];
            let status = if device.unavailable.is_some() {
                "路径不可用"
            } else {
                match session["lifecycle"].as_str() {
                    Some("starting") => "正在上电",
                    Some("stopping") => "正在下电",
                    Some("resetting") => "正在复位",
                    Some("failed") => "启动失败",
                    Some("on") if session["guest_fault"] == true => "固件异常",
                    Some("on") => "已上电",
                    _ => "已下电",
                }
            };
            let weak = cx.entity().downgrade();
            let menu_device = device.clone();
            let context_weak = weak.clone();
            let context_device = device.clone();
            let enabled = device.unavailable.is_none();
            let row = div()
                .id(SharedString::from(format!("instance-{id}")))
                .px_6()
                .py_5()
                .flex()
                .items_center()
                .gap_4()
                .border_b_1()
                .border_color(cx.theme().border)
                .cursor_pointer()
                .hover(|style| style.bg(cx.theme().secondary))
                .when(self.selected.as_ref() == Some(&id), |row| {
                    row.bg(cx.theme().secondary)
                })
                .child(
                    div()
                        .flex_1()
                        .min_w_0()
                        .flex()
                        .flex_col()
                        .gap_2()
                        .child(div().text_lg().truncate().child(device.name.clone()))
                        .child(caption(format!(
                            "{} · {status}",
                            device.hardware["board"]["name"]
                                .as_str()
                                .unwrap_or("未知板型")
                        ))),
                )
                .child(
                    Button::new(SharedString::from(format!("settings-{id}")))
                        .ghost()
                        .icon(gpui::assets::IconName::Settings)
                        .accessibility_label(format!("{} · 设置", device.name))
                        .tooltip("设置")
                        .disabled(!enabled)
                        .on_click(cx.listener(move |view, _, window, cx| {
                            cx.stop_propagation();
                            view.select_instance(&settings_id);
                            view.act(Action::Settings, window, cx);
                        })),
                )
                .child(
                    Button::new(SharedString::from(format!("more-{id}")))
                        .ghost()
                        .icon(Icon::empty().path("more"))
                        .accessibility_label(format!("{} · 更多操作", device.name))
                        .tooltip("更多操作")
                        .dropdown_menu_with_anchor(gpui::Anchor::TopRight, move |menu, _, cx| {
                            if let Some(view) = weak.upgrade() {
                                view.update(cx, |v, cx| v.instance_menu(menu, &menu_device, cx))
                            } else {
                                menu
                            }
                        }),
                )
                .on_click(
                    cx.listener(move |view, event: &gpui::ClickEvent, window, cx| {
                        view.select_instance(&id);
                        if enabled && event.click_count() == 2 {
                            view.act(Action::Open, window, cx);
                        }
                        cx.notify();
                    }),
                )
                .context_menu(move |menu, _, cx| {
                    if let Some(view) = context_weak.upgrade() {
                        view.update(cx, |v, cx| v.instance_menu(menu, &context_device, cx))
                    } else {
                        menu
                    }
                });
            list = list.child(row);
        }
        div().flex_1().min_w_0().flex().flex_col().child(list)
    }

    pub(super) fn wifi_panel(&self, device: &Device, cx: &mut Context<Self>) -> Div {
        div()
            .flex_1()
            .min_w_0()
            .flex()
            .flex_col()
            .p_6()
            .gap_4()
            .child(
                div()
                    .flex()
                    .items_center()
                    .gap_3()
                    .child(
                        div()
                            .flex_1()
                            .text_lg()
                            .child(format!("{} · Wi-Fi", device.name)),
                    )
                    .child(caption(self.lifecycle()))
                    .child(self.control("返回设备", "wifi-back", Action::Cancel, true, false, cx)),
            )
            .child(self.wifi.as_ref().unwrap().clone())
    }
}
