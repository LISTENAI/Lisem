//! Per-instance logical Wi-Fi environment editor.
use crate::backend::{Backend, Device};
use gpui::component::{
    ActiveTheme, Disableable, scroll::ScrollableElement,
    button::{Button, ButtonVariants},
    input::{Input, InputEvent, InputState},
    menu::{DropdownMenu, PopupMenuItem},
    switch::Switch,
};
use gpui::{Context, Entity, Render, Window, div, prelude::*, px};
use lisem_core::wifi_environment::{
    AccessPoint, MAX_ACCESS_POINTS, Security, WifiEnvironment as Configuration,
};
use serde_json::{Value, json};
use std::{sync::Arc, time::Duration};

struct Row {
    ssid: Entity<InputState>,
    rssi: Entity<InputState>,
    channel: Entity<InputState>,
    internet: bool,
    security: Security,
    password: Entity<InputState>,
    saved_index: Option<usize>,
}

pub struct WifiEnvironment {
    backend: Arc<Backend>,
    id: String,
    rows: Vec<Row>,
    saved: Configuration,
    pending: bool,
    message: String,
    observed: Value,
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
            .enumerate()
            .map(|(index, ap)| Self::row(ap, Some(index), window, cx))
            .collect();
        cx.spawn(async move |view, cx| {
            loop {
                cx.background_executor()
                    .timer(Duration::from_millis(250))
                    .await;
                if view
                    .update(cx, |view, cx| {
                        let current = view.live_wifi();
                        if current != view.observed {
                            view.observed = current;
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
        Self {
            backend,
            id: device.id,
            rows,
            saved,
            pending: false,
            message: String::new(),
            observed: Value::Null,
        }
    }

    fn live_wifi(&self) -> Value {
        let snapshot = self.backend.snapshot.lock().unwrap();
        let session = &snapshot.data["sessions"][&self.id];
        if session["finished"] == false {
            session["wifi"].clone()
        } else {
            Value::Null
        }
    }

    fn row(
        ap: &AccessPoint,
        saved_index: Option<usize>,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) -> Row {
        let row = Row {
            saved_index,
            ssid: cx.new(|cx| {
                InputState::new(window, cx)
                    .default_value(&ap.ssid)
                    .placeholder("热点名称")
            }),
            rssi: cx.new(|cx| InputState::new(window, cx).default_value(ap.rssi.to_string())),
            channel: cx.new(|cx| InputState::new(window, cx).default_value(ap.channel.to_string())),
            internet: ap.internet,
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
                internet: row.internet,
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
        if self.pending {
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
        let run = {
            let snapshot = self.backend.snapshot.lock().unwrap();
            let session = &snapshot.data["sessions"][&self.id];
            if session["finished"] == false {
                session["output"].clone()
            } else {
                serde_json::Value::Null
            }
        };
        let receiver = self.backend.query(
            "wifi_environment",
            json!({"id":self.id,"run":run,"environment":configuration}),
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
                        for (index, row) in view.rows.iter_mut().enumerate() {
                            row.saved_index = Some(index);
                        }
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
        let locked = self.pending;
        let dirty = self
            .configuration(cx)
            .as_ref()
            .map(|v| v != &self.saved)
            .unwrap_or(true);
        let live = self.live_wifi();
        // Match observations to saved rows, retaining identity during draft edits.
        let matches_saved = live["access_points"].as_array().is_some_and(|aps| {
            aps.len() == self.saved.access_points.len()
                && aps
                    .iter()
                    .zip(&self.saved.access_points)
                    .all(|(live, saved)| {
                        live["ssid"] == saved.ssid
                            && live["rssi"] == saved.rssi
                            && live["channel"] == saved.channel
                            && live["internet"] == saved.internet
                            && (live["security"] == "wpa2-psk")
                                == (saved.security == Security::Wpa2Psk)
                    })
        });
        let connected = live["access_points"]
            .as_array()
            .and_then(|aps| aps.iter().find(|ap| ap["state"] == "connected"));
        let connection_label = connected
            .map(|ap| format!("已连接 {}", ap["ssid"].as_str().unwrap_or("")))
            .unwrap_or_else(|| {
                if live.is_null() {
                    "未运行".into()
                } else {
                    "未连接".into()
                }
            });
        let mut rows = div().flex().flex_col();
        for (index, row) in self.rows.iter().enumerate() {
            let owner = cx.entity().downgrade();
            let security = row.security;
            let ap = row
                .saved_index
                .filter(|_| matches_saved)
                .map(|index| &live["access_points"][index])
                .unwrap_or(&Value::Null);
            let connected = ap["state"] == "connected";
            let state = match ap["state"].as_str() {
                Some("connected") => "已连接",
                Some("authenticating") => "认证中",
                Some("unavailable") => "不可用",
                _ => "",
            };
            let identity = ap["bssid"].as_str().map(str::to_owned).unwrap_or_else(|| {
                if row.saved_index.is_none() {
                    "新热点".into()
                } else {
                    String::new()
                }
            });
            rows = rows.child(
                div()
                    .py_2()
                    .flex_shrink_0()
                    .flex()
                    .flex_col()
                    .gap_2()
                    .border_b_1()
                    .border_color(cx.theme().border)
                    .child(
                        div()
                            .flex()
                            .items_start()
                            .gap_3()
                            .child(
                                div()
                                    .flex_1()
                                    .min_w_0()
                                    .flex()
                                    .flex_col()
                                    .gap_1()
                                    .child(Input::new(&row.ssid).disabled(locked))
                                    .when(!identity.is_empty(), |view| {
                                        view.child(
                                            div()
                                                .h(px(18.))
                                                .flex()
                                                .items_center()
                                                .gap_2()
                                                .text_xs()
                                                .text_color(cx.theme().muted_foreground)
                                                .child(div().truncate().child(identity))
                                                .when(!state.is_empty(), |v| {
                                                    v.child(
                                                        div()
                                                            .flex_shrink_0()
                                                            .text_color(if connected {
                                                                cx.theme().primary
                                                            } else {
                                                                cx.theme().muted_foreground
                                                            })
                                                            .child(state),
                                                    )
                                                }),
                                        )
                                    }),
                            )
                            .child(
                                div()
                                    .w(px(80.))
                                    .flex_shrink_0()
                                    .child(Input::new(&row.rssi).disabled(locked)),
                            )
                            .child(
                                div()
                                    .w(px(60.))
                                    .flex_shrink_0()
                                    .child(Input::new(&row.channel).disabled(locked)),
                            )
                            .child(
                                div()
                                    .w(px(64.))
                                    .h(px(32.))
                                    .flex_shrink_0()
                                    .flex()
                                    .items_center()
                                    .justify_center()
                                    .child(
                                        Switch::new(("wifi-internet", index))
                                            .checked(row.internet)
                                            .disabled(locked)
                                            .on_click(cx.listener(move |view, enabled, _, cx| {
                                                view.rows[index].internet = *enabled;
                                                view.message.clear();
                                                cx.notify();
                                            })),
                                    ),
                            )
                            .child(
                                Button::new(("wifi-remove", index))
                                    .label("移除")
                                    .w(px(56.))
                                    .ghost()
                                    .tooltip("移除热点")
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
                            .gap_2()
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
                                                            if view.pending {
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
                            .when(security == Security::Wpa2Psk, |v| {
                                v.child(div().flex_1().min_w_0().child(
                                    Input::new(&row.password).mask_toggle().disabled(locked),
                                ))
                            }),
                    ),
            );
        }
        div()
            .id("wifi-environment")
            .size_full()
            .min_h_0()
            .overflow_y_scrollbar()
            .flex()
            .flex_col()
            .gap_3()
            .child(
                div()
                    .flex()
                    .items_center()
                    .justify_between()
                    .gap_3()
                    .child(
                        div()
                            .flex_1()
                            .min_w_0()
                            .truncate()
                            .text_sm()
                            .text_color(cx.theme().muted_foreground)
                            .child(connection_label),
                    )
                    .child(
                        Button::new("wifi-add")
                            .label("添加热点")
                            .icon(gpui::assets::IconName::Plus)
                            .disabled(locked || self.rows.len() >= MAX_ACCESS_POINTS)
                            .on_click(cx.listener(|view, _, window, cx| {
                                view.rows.push(Self::row(
                                    &AccessPoint {
                                        ssid: "".into(),
                                        rssi: -60,
                                        channel: 1,
                                        internet: true,
                                        security: Security::Open,
                                        password: String::new(),
                                    },
                                    None,
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
                        .text_xs()
                        .text_color(cx.theme().muted_foreground)
                        .child(div().flex_1().child("名称 / BSSID"))
                        .child(div().w(px(80.)).child("RSSI · dBm"))
                        .child(div().w(px(60.)).child("信道"))
                        .child(div().w(px(64.)).text_center().child("互联网"))
                        .child(div().w(px(56.))),
                )
            })
            .child(
                div()
                    .id("wifi-hotspot-list")
                    .flex_shrink_0()
                    .child(rows)
                    .when(self.rows.is_empty(), |v| {
                        v.child(
                            div()
                                .py_12()
                                .text_center()
                                .text_sm()
                                .text_color(cx.theme().muted_foreground)
                                .child("没有热点"),
                        )
                    }),
            )
            .child(
                div()
                    .flex_shrink_0()
                    .pt_1()
                    .flex()
                    .items_center()
                    .gap_2()
                    .child(
                        div()
                            .flex_1()
                            .min_w_0()
                            .text_sm()
                            .text_color(cx.theme().muted_foreground)
                            .child(if self.message.is_empty() && dirty {
                                "未保存".to_owned()
                            } else {
                                self.message.clone()
                            }),
                    )
                    .child(
                        Button::new("wifi-revert")
                            .label("还原")
                            .ghost()
                            .disabled(locked || !dirty)
                            .on_click(cx.listener(|view, _, window, cx| {
                                view.rows = view
                                    .saved
                                    .access_points
                                    .iter()
                                    .enumerate()
                                    .map(|(index, ap)| Self::row(ap, Some(index), window, cx))
                                    .collect();
                                view.message.clear();
                                cx.notify();
                            })),
                    )
                    .child(
                        Button::new("wifi-save")
                            .label(if locked { "保存中…" } else { "保存" })
                            .primary()
                            .disabled(locked || !dirty)
                            .on_click(cx.listener(|view, _, _, cx| view.save(cx))),
                    ),
            )
    }
}
