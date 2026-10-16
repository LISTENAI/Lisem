//! Width-aware device controls. Collapsed entries retain the same live menus.
use crate::*;

struct Layout {
    peripherals: usize,
    indicators: bool,
}

impl Layout {
    fn for_width(width: f32, chrome: f32, peripherals: usize, indicators: usize) -> Self {
        // Reserve the instance name, padding, and lifecycle controls.
        let slots = ((width - chrome - 100. - 32. - 12.) / 36.).floor().max(3.) as usize;
        Self {
            peripherals: peripherals.min(slots - 3),
            indicators: slots >= peripherals + indicators + 3,
        }
    }
}

impl Desktop {
    pub(super) fn device_toolbar(
        &self,
        d: &Device,
        indicators: &[Value],
        pa_enabled: bool,
        window: &Window,
        cx: &mut Context<Self>,
    ) -> impl IntoElement {
        let on = self.powered();
        let idle = !self.control_busy(&[]);
        let mut peripherals = Vec::new();
        if d.capabilities.camera {
            peripherals.push((
                "camera",
                "摄像头",
                on && self.state.data["session"]["camera_input"]["receiver_enabled"] == true,
            ));
        }
        if d.capabilities.audio {
            peripherals.push((
                "microphone",
                "麦克风",
                on && self.state.data["session"]["audio_input_enabled"] == true,
            ));
            peripherals.push(("speaker", "扬声器", pa_enabled));
        }
        if d.capabilities.wifi {
            peripherals.push(("network", "Wi-Fi", self.access_point_present(d)));
        }
        if d.capabilities.sd {
            peripherals.push(("sd", "SD 卡", self.sd_medium(d)["path"].is_string()));
        }
        if d.capabilities.usb_adb {
            peripherals.push(("usb", "USB", self.usb_configured()));
        }
        if !d.capabilities.uart_channels.is_empty() {
            peripherals.push(("serial", "串口", false));
        }
        let statuses: Vec<_> = indicators
            .iter()
            .filter(|item| item["kind"] == "led" || item["kind"] == "signal")
            .map(|item| {
                let id = item["id"].as_str().unwrap_or_default().to_owned();
                let label = item["label"].as_str().unwrap_or(&id).to_owned();
                let state = self.state.data["session"]["indicators"][&id].as_bool();
                let lit = on && state == Some(true);
                let status = if !on {
                    "已下电"
                } else {
                    match state {
                        Some(true) => "有效",
                        Some(false) => "无效",
                        None => "未驱动",
                    }
                };
                let color = item["color"]
                    .as_str()
                    .and_then(|v| u32::from_str_radix(v.trim_start_matches('#'), 16).ok())
                    .unwrap_or(0x4cc3a2);
                (id, format!("{label} · {status}"), lit, color)
            })
            .collect();
        let insets = gpui::component::window_paddings(window);
        let width = f32::from(window.viewport_size().width - insets.left - insets.right);
        let chrome = platform::header_reserved_width(window);
        let layout = Layout::for_width(width, chrome, peripherals.len(), statuses.len());
        let hidden = peripherals[layout.peripherals..].to_vec();
        let mut controls = div()
            .occlude()
            .flex_shrink_0()
            .flex()
            .items_center()
            .gap_1();
        if layout.indicators {
            for (id, tooltip, lit, color) in &statuses {
                let tooltip = tooltip.clone();
                controls = controls.child(
                    div()
                        .id(SharedString::from(format!("indicator-{id}")))
                        .size(px(32.))
                        .flex_shrink_0()
                        .flex()
                        .items_center()
                        .justify_center()
                        .tooltip(move |w, cx| Tooltip::new(tooltip.clone()).build(w, cx))
                        .child(
                            div()
                                .size(px(10.))
                                .rounded_full()
                                .border_1()
                                .border_color(if *lit {
                                    rgb(*color).into()
                                } else {
                                    cx.theme().border
                                })
                                .bg(if *lit {
                                    rgb(*color).into()
                                } else {
                                    cx.theme().muted
                                }),
                        ),
                );
            }
        }
        for (kind, _, active) in &peripherals[..layout.peripherals] {
            controls = controls.child(self.peripheral_menu(kind, *active, cx));
        }
        let view = cx.entity().downgrade();
        let more = Button::new("device-more")
            .ghost()
            .w(px(32.))
            .h(px(32.))
            .icon(Icon::empty().path("more"))
            .accessibility_label("更多")
            .tooltip("更多")
            .dropdown_menu_with_anchor(gpui::Anchor::TopRight, move |mut menu, window, cx| {
                for (kind, label, _) in &hidden {
                    let weak = view.clone();
                    let kind = *kind;
                    menu = menu.submenu(*label, window, cx, move |menu, _, cx| {
                        let Some(view) = weak.upgrade() else {
                            return menu;
                        };
                        view.update(cx, |v, cx| v.populate_peripheral_menu(kind, menu, cx))
                    });
                }
                if !layout.indicators && !statuses.is_empty() {
                    let statuses = statuses.clone();
                    menu = menu.submenu("指示灯", window, cx, move |mut menu, _, _| {
                        for (_, label, lit, _) in &statuses {
                            menu = menu.item(
                                PopupMenuItem::new(label.clone())
                                    .checked(*lit)
                                    .disabled(true),
                            );
                        }
                        menu
                    });
                }
                menu = menu.separator();
                let items = view
                    .upgrade()
                    .map(|v| v.read(cx).menu_items(Action::Details))
                    .unwrap_or_default();
                for (label, action, enabled, checked) in items {
                    let weak = view.clone();
                    menu = menu.item(
                        PopupMenuItem::new(label)
                            .disabled(!enabled)
                            .checked(checked)
                            .on_click(move |_, w, cx| {
                                let _ = weak.update(cx, |v, cx| v.act(action, w, cx));
                            }),
                    );
                }
                menu
            });
        controls = controls
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
            .child(self.icon("reset", "复位", Action::Reset, idle && on, false, cx))
            .child(more);
        TitleBar::new()
            .on_close_window(cx.listener(|view, _, w, _| {
                view.release_buttons();
                w.remove_window();
            }))
            .h(px(52.))
            .bg(cx.theme().background)
            .child(
                div()
                    .flex_1()
                    .min_w_0()
                    .px_4()
                    .flex()
                    .items_center()
                    .gap_3()
                    .child(
                        div()
                            .flex_1()
                            .min_w_0()
                            .truncate()
                            .text_lg()
                            .child(d.name.clone()),
                    )
                    .child(controls),
            )
    }
}

#[cfg(test)]
mod tests {
    use super::Layout;

    #[test]
    fn controls_fit_with_many_board_indicators() {
        for width in [640., 760., 1000., 1400.] {
            for indicators in [0, 4, 32] {
                let layout = Layout::for_width(width, 206., 7, indicators);
                let slots = 3 + layout.peripherals + if layout.indicators { indicators } else { 0 };
                assert!(slots as f32 * 36. + 206. + 100. + 32. + 12. <= width);
            }
        }
    }
}
