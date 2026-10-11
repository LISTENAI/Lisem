//! Per-instance logical Wi-Fi environment editor.
use crate::backend::{Backend, Device};
use gpui::component::{
    ActiveTheme, Disableable,
    button::{Button, ButtonVariants},
    input::{Input, InputEvent, InputState},
    menu::{DropdownMenu, PopupMenuItem},
    switch::Switch,
};
use gpui::{Context, Entity, Render, Window, div, prelude::*, px};
use lisem_core::wifi_environment::{
    AccessPoint, MAX_ACCESS_POINTS, Security, WifiEnvironment as Configuration,
};
use serde_json::json;
use std::{sync::Arc, time::Duration};

struct Row {
    ssid: Entity<InputState>,
    rssi: Entity<InputState>,
    channel: Entity<InputState>,
    connectable: bool,
    security: Security,
    password: Entity<InputState>,
}

pub struct WifiEnvironment {
    backend: Arc<Backend>,
    id: String,
    rows: Vec<Row>,
    saved: Configuration,
    pending: bool,
    message: String,
}

impl WifiEnvironment {
    pub fn new(
        backend: Arc<Backend>,
        device: Device,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) -> Self {
        let saved = Configuration::from_value(&device.environment["wifi"]).unwrap_or_default();
        let rows = saved
            .access_points
            .iter()
            .map(|ap| Self::row(ap, window, cx))
            .collect();
        Self {
            backend,
            id: device.id,
            rows,
            saved,
            pending: false,
            message: String::new(),
        }
    }

    fn row(ap: &AccessPoint, window: &mut Window, cx: &mut Context<Self>) -> Row {
        let row = Row {
            ssid: cx.new(|cx| {
                InputState::new(window, cx)
                    .default_value(&ap.ssid)
                    .placeholder("热点名称")
            }),
            rssi: cx.new(|cx| InputState::new(window, cx).default_value(ap.rssi.to_string())),
            channel: cx.new(|cx| InputState::new(window, cx).default_value(ap.channel.to_string())),
            connectable: ap.connectable,
            security: ap.security,
            password: cx.new(|cx| {
                InputState::new(window, cx)
                    .default_value(&ap.password)
                    .masked(true)
                    .placeholder("密码（8–63 字符）")
            }),
        };
        for input in [&row.ssid, &row.rssi, &row.channel, &row.password] {
            cx.subscribe(input, |view, _, event, cx| {
                if matches!(event, InputEvent::Change) {
                    view.message.clear();
                    cx.notify();
                }
            })
            .detach();
        }
        row
    }

    fn powered(&self) -> bool {
        self.backend.snapshot.lock().unwrap().data["sessions"][&self.id]["finished"] == false
    }

    fn configuration(&self, cx: &Context<Self>) -> Result<Configuration, String> {
        let mut access_points = Vec::new();
        for (i, row) in self.rows.iter().enumerate() {
            let ssid = row.ssid.read(cx).value().to_string();
            if ssid.is_empty() || ssid.len() > 32 || ssid.contains('\0') {
                return Err(format!("第 {} 个热点的名称需为 1–32 字节", i + 1));
            }
            let rssi = row
                .rssi
                .read(cx)
                .value()
                .parse::<i16>()
                .ok()
                .filter(|r| (-127..=0).contains(r))
                .ok_or_else(|| format!("第 {} 个热点的 RSSI 需为 −127–0 dBm", i + 1))?;
            let channel = row
                .channel
                .read(cx)
                .value()
                .parse::<u8>()
                .ok()
                .filter(|c| (1..=14).contains(c))
                .ok_or_else(|| format!("第 {} 个热点的信道需为 1–14", i + 1))?;
            access_points.push(AccessPoint {
                ssid,
                rssi,
                channel,
                connectable: row.connectable,
                security: row.security,
                password: if row.security == Security::Open {
                    String::new()
                } else {
                    row.password.read(cx).value().to_string()
                },
            });
        }
        let configuration = Configuration { access_points };
        configuration
            .validate()
            .map_err(|_| "WPA2 密码需为 8–63 个可打印 ASCII 字符".to_owned())?;
        Ok(configuration)
    }

    fn save(&mut self, cx: &mut Context<Self>) {
        if self.pending || self.powered() {
            return;
        }
        let configuration = match self.configuration(cx) {
            Ok(value) => value,
            Err(message) => {
                self.message = message;
                cx.notify();
                return;
            }
        };
        let receiver = self.backend.query(
            "wifi_environment",
            json!({"id":self.id,"environment":configuration}),
        );
        self.pending = true;
        self.message.clear();
        cx.notify();
        cx.spawn(async move |view, cx| {
            let result = cx
                .background_executor()
                .spawn(async move {
                    receiver
                        .recv_timeout(Duration::from_secs(65))
                        .unwrap_or_else(|_| Err("保存超时，请重新打开页面核对配置".into()))
                })
                .await;
            let _ = view.update(cx, |view, cx| {
                view.pending = false;
                match result {
                    Ok(_) => {
                        view.saved = configuration;
                        view.message = "已保存".into();
                    }
                    Err(error) => view.message = error,
                }
                cx.notify();
            });
        })
        .detach();
    }
}

impl Render for WifiEnvironment {
    fn render(&mut self, _: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let powered = self.powered();
        let locked = powered || self.pending;
        let dirty = self
            .configuration(cx)
            .as_ref()
            .map(|v| v != &self.saved)
            .unwrap_or(true);
        let mut rows = div().flex().flex_col().gap_2();
        for (index, row) in self.rows.iter().enumerate() {
            let owner = cx.entity().downgrade();
            let security = row.security;
            rows = rows.child(
                div()
                    .flex()
                    .flex_col()
                    .gap_2()
                    .pb_3()
                    .child(
                        div()
                            .flex()
                            .items_center()
                            .gap_3()
                            .child(
                                div()
                                    .flex_1()
                                    .min_w_0()
                                    .child(Input::new(&row.ssid).disabled(locked)),
                            )
                            .child(
                                div()
                                    .w(px(86.))
                                    .child(Input::new(&row.rssi).disabled(locked)),
                            )
                            .child(
                                div()
                                    .w(px(68.))
                                    .child(Input::new(&row.channel).disabled(locked)),
                            )
                            .child(
                                div().w(px(74.)).flex().justify_center().child(
                                    Switch::new(("wifi-connectable", index))
                                        .checked(row.connectable)
                                        .disabled(locked)
                                        .on_click(cx.listener(move |view, enabled, _, cx| {
                                            view.rows[index].connectable = *enabled;
                                            view.message.clear();
                                            cx.notify();
                                        })),
                                ),
                            )
                            .child(
                                Button::new(("wifi-remove", index))
                                    .label("移除")
                                    .ghost()
                                    .disabled(locked)
                                    .on_click(cx.listener(move |view, _, _, cx| {
                                        view.rows.remove(index);
                                        view.message.clear();
                                        cx.notify();
                                    })),
                            ),
                    )
                    .child(
                        div()
                            .flex()
                            .items_center()
                            .gap_3()
                            .child(
                                Button::new(("wifi-security", index))
                                    .label(if security == Security::Open {
                                        "开放"
                                    } else {
                                        "WPA2-Personal"
                                    })
                                    .icon(gpui::component::Icon::new(
                                        gpui::assets::IconName::ChevronDown,
                                    ))
                                    .disabled(locked)
                                    .dropdown_menu(move |mut menu, _, _| {
                                        for (label, choice) in [
                                            ("开放", Security::Open),
                                            ("WPA2-Personal", Security::Wpa2Psk),
                                        ] {
                                            let owner = owner.clone();
                                            menu = menu.item(
                                                PopupMenuItem::new(label)
                                                    .checked(choice == security)
                                                    .on_click(move |_, _, cx| {
                                                        let _ = owner.update(cx, |view, cx| {
                                                            if view.pending || view.powered() {
                                                                return;
                                                            }
                                                            if let Some(row) =
                                                                view.rows.get_mut(index)
                                                            {
                                                                row.security = choice;
                                                            }
                                                            view.message.clear();
                                                            cx.notify();
                                                        });
                                                    }),
                                            );
                                        }
                                        menu
                                    }),
                            )
                            .when(security == Security::Wpa2Psk, |view| {
                                view.child(div().flex_1().min_w_0().child(
                                    Input::new(&row.password).mask_toggle().disabled(locked),
                                ))
                            }),
                    ),
            );
        }
        div()
            .id("wifi-environment")
            .size_full()
            .overflow_y_scroll()
            .p_5()
            .flex()
            .flex_col()
            .gap_4()
            .child(
                div()
                    .flex()
                    .items_center()
                    .justify_between()
                    .child(div().text_lg().child("周围热点"))
                    .child(
                        Button::new("wifi-add")
                            .label("添加热点")
                            .disabled(locked || self.rows.len() >= MAX_ACCESS_POINTS)
                            .on_click(cx.listener(|view, _, window, cx| {
                                view.rows.push(Self::row(
                                    &AccessPoint {
                                        ssid: "".into(),
                                        rssi: -60,
                                        channel: 1,
                                        connectable: true,
                                        security: Security::Open,
                                        password: String::new(),
                                    },
                                    window,
                                    cx,
                                ));
                                view.message.clear();
                                cx.notify();
                            })),
                    ),
            )
            .when(!self.rows.is_empty(), |view| {
                view.child(
                    div()
                        .flex()
                        .items_center()
                        .gap_3()
                        .text_sm()
                        .text_color(cx.theme().muted_foreground)
                        .child(div().flex_1().child("名称"))
                        .child(div().w(px(86.)).child("RSSI · dBm"))
                        .child(div().w(px(68.)).child("信道"))
                        .child(div().w(px(74.)).text_center().child("允许连接"))
                        .child(div().w(px(56.))),
                )
            })
            .child(rows)
            .when(self.rows.is_empty(), |view| {
                view.child(
                    div()
                        .py_6()
                        .text_sm()
                        .text_color(cx.theme().muted_foreground)
                        .child("没有热点，固件将搜不到网络。"),
                )
            })
            .child(
                div()
                    .text_xs()
                    .text_color(cx.theme().muted_foreground)
                    .child("信道写入热点广播；当前不模拟射频调谐、干扰和信号衰减。"),
            )
            .child(
                div()
                    .flex()
                    .items_center()
                    .gap_3()
                    .child(
                        Button::new("wifi-save")
                            .label(if self.pending {
                                "保存中…"
                            } else {
                                "保存"
                            })
                            .primary()
                            .disabled(locked || !dirty)
                            .on_click(cx.listener(|view, _, _, cx| view.save(cx))),
                    )
                    .child(
                        div()
                            .text_sm()
                            .text_color(cx.theme().muted_foreground)
                            .child(if powered {
                                "下电后可修改".to_owned()
                            } else {
                                self.message.clone()
                            }),
                    ),
            )
    }
}
