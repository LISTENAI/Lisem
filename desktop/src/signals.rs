//! Editable typed inputs for the selected board.
use crate::*;
use gpui::component::{
    input::{InputEvent, NumberInput, NumberInputEvent, NumberStep, StepAction},
    slider::{Slider, SliderEvent, SliderState},
};
use std::{cell::Cell, collections::BTreeMap};

struct VoltageField {
    id: String,
    input: Entity<InputState>,
    slider: Entity<SliderState>,
    observed: String,
    dragging: Rc<Cell<bool>>,
}

pub struct Signals {
    backend: Arc<Backend>,
    id: String,
    fields: Vec<VoltageField>,
    pending: Option<(String, Value)>,
    queued: BTreeMap<String, (Value, Value)>,
    message: String,
}

impl Signals {
    pub fn new(
        backend: Arc<Backend>,
        device: Device,
        window: &mut Window,
        cx: &mut Context<Self>,
    ) -> Self {
        let fields = device
            .inputs
            .as_array()
            .into_iter()
            .flatten()
            .filter(|row| row["type"] == "voltage")
            .map(|row| {
                let id = row["id"].as_str().unwrap().to_owned();
                let max = row["max"].as_u64().unwrap() as u32;
                let input = cx.new(|cx| {
                    let mut input =
                        InputState::new(window, cx).default_value(row["value"].to_string());
                    input.set_step(None::<NumberStep>, window, cx);
                    input
                });
                let slider = cx.new(|_| {
                    SliderState::new()
                        .min(0.)
                        .max(max as f32)
                        .step(10.)
                        .default_value(row["value"].as_u64().unwrap() as f32)
                });
                let target = id.clone();
                let text = input.clone();
                cx.subscribe_in(
                    &slider,
                    window,
                    move |view: &mut Self, _, event, window, cx| {
                        if let SliderEvent::Change(value) = event {
                            let value = value.start().round() as u32;
                            text.update(cx, |input, cx| {
                                input.set_value(value.to_string(), window, cx)
                            });
                            view.apply(target.clone(), json!(value), cx);
                        }
                    },
                )
                .detach();
                let target = id.clone();
                cx.subscribe_in(
                    &input,
                    window,
                    move |view: &mut Self, field, event, window, cx| {
                        if matches!(event, InputEvent::PressEnter { .. } | InputEvent::Blur) {
                            match field.read(cx).value().trim().parse::<u32>() {
                                Ok(value) if value <= max => {
                                    view.set_voltage(&target, value, window, cx);
                                }
                                _ => {
                                    view.message = format!("请输入 0–{max} mV 的整数电压值");
                                    cx.notify();
                                }
                            }
                        }
                    },
                )
                .detach();
                let target = id.clone();
                cx.subscribe_in(
                    &input,
                    window,
                    move |view: &mut Self, field, event, window, cx| {
                        let NumberInputEvent::Step(action) = event;
                        if let Ok(value) = field.read(cx).value().trim().parse::<u32>() {
                            let value = match action {
                                StepAction::Increment => value.saturating_add(10).min(max),
                                StepAction::Decrement => value.saturating_sub(10).min(max),
                            };
                            view.set_voltage(&target, value, window, cx);
                        }
                    },
                )
                .detach();
                VoltageField {
                    id,
                    input,
                    slider,
                    observed: row["value"].to_string(),
                    dragging: Rc::new(Cell::new(false)),
                }
            })
            .collect();
        cx.spawn(async move |view, cx| {
            loop {
                cx.background_executor()
                    .timer(Duration::from_millis(250))
                    .await;
                if view.update(cx, |_, cx| cx.notify()).is_err() {
                    break;
                }
            }
        })
        .detach();
        Self {
            backend,
            id: device.id,
            fields,
            pending: None,
            queued: BTreeMap::new(),
            message: String::new(),
        }
    }

    fn set_voltage(&mut self, id: &str, value: u32, window: &mut Window, cx: &mut Context<Self>) {
        if let Some(field) = self.fields.iter().find(|field| field.id == id) {
            field.input.update(cx, |input, cx| {
                input.set_value(value.to_string(), window, cx)
            });
            field
                .slider
                .update(cx, |slider, cx| slider.set_value(value as f32, window, cx));
        }
        self.apply(id.to_owned(), json!(value), cx);
    }

    fn state(&self) -> (Value, Value) {
        let snapshot = self.backend.snapshot.lock().unwrap();
        let session = &snapshot.data["sessions"][&self.id];
        if session["finished"] == false {
            (session["signals"].clone(), session["output"].clone())
        } else {
            let inputs = snapshot.data["devices"]
                .as_array()
                .into_iter()
                .flatten()
                .find(|d| d["id"] == self.id)
                .map(|d| d["inputs"].clone())
                .unwrap_or_default();
            (inputs, Value::Null)
        }
    }

    fn apply(&mut self, id: String, value: Value, cx: &mut Context<Self>) {
        let (inputs, run) = self.state();
        if self.pending.is_none()
            && !value.is_null()
            && inputs
                .as_array()
                .into_iter()
                .flatten()
                .any(|row| row["id"] == id && row["value"] == value)
        {
            self.message.clear();
            cx.notify();
            return;
        }
        if let Some((pending_id, pending_value)) = &self.pending {
            if pending_id == &id && pending_value == &value {
                self.queued.remove(&id);
            } else {
                self.queued.insert(id, (value, run));
            }
            return;
        }
        self.pending = Some((id.clone(), value.clone()));
        let receiver = self.backend.query(
            "signals",
            json!({"id":self.id,"run":run,"changes":{id:value}}),
        );
        let backend = self.backend.clone();
        let device = self.id.clone();
        self.message = "正在应用…".into();
        cx.notify();
        cx.spawn(async move |view, cx| {
            let result = cx
                .background_executor()
                .spawn(async move {
                    let reply = receiver
                        .recv_timeout(Duration::from_secs(65))
                        .map_err(|_| "输入修改未确认，请检查当前值".to_owned())??;
                    if reply["status"] != "pending" {
                        return Ok(reply);
                    }
                    let request = reply["request"].clone();
                    let deadline = std::time::Instant::now() + Duration::from_secs(15);
                    loop {
                        let reply = backend
                            .query("signals", json!({"id":device,"run":run}))
                            .recv_timeout(Duration::from_secs(5))
                            .map_err(|_| "查询输入状态超时".to_owned())??;
                        if reply["run"] != run {
                            return Err("设备运行已结束，请检查当前值".into());
                        }
                        if reply["change"]["request"] == request
                            && reply["change"]["status"] != "pending"
                        {
                            return Ok(reply["change"].clone());
                        }
                        if std::time::Instant::now() >= deadline {
                            return Err("输入修改未确认，请检查当前值".into());
                        }
                        std::thread::sleep(Duration::from_millis(200));
                    }
                })
                .await;
            let _ = view.update(cx, |view, cx| {
                view.pending = None;
                let confirmed = result
                    .as_ref()
                    .is_ok_and(|reply| reply["status"] == "applied" || reply["status"] == "saved");
                view.message = match result {
                    Ok(reply) if reply["error"].is_string() => {
                        reply["error"].as_str().unwrap().into()
                    }
                    Ok(reply) if reply["status"] == "applied" => String::new(),
                    Ok(reply) if reply["status"] == "saved" => String::new(),
                    Ok(_) => "输入修改未确认，请检查当前值".into(),
                    Err(error) => error,
                };
                if confirmed {
                    if let Some((id, (value, run))) = view.queued.pop_first() {
                        if view.state().1 == run {
                            view.apply(id, value, cx);
                        } else {
                            view.queued.clear();
                            view.message = "设备运行已变化，请重新设置输入".into();
                        }
                    }
                } else {
                    view.queued.clear();
                }
                cx.notify();
            });
        })
        .detach();
    }
}

impl VoltageField {
    fn control(&self) -> impl IntoElement {
        let pressed = self.dragging.clone();
        let state = self.slider.clone();
        div().relative().child(Slider::new(&self.slider)).child(
            gpui::canvas(
                |bounds, window, _| window.insert_hitbox(bounds, gpui::HitboxBehavior::Normal),
                move |_, hitbox, window, _| {
                    let down = pressed.clone();
                    window.on_mouse_event(move |event: &gpui::MouseDownEvent, phase, window, _| {
                        if phase.capture() && event.button == MouseButton::Left {
                            down.set(hitbox.is_hovered(window));
                        }
                    });
                    let pressed = pressed.clone();
                    let state = state.clone();
                    window.on_mouse_event(move |event: &gpui::MouseUpEvent, phase, window, cx| {
                        if phase.capture()
                            && event.button == MouseButton::Left
                            && pressed.replace(false)
                        {
                            // A coalesced move can only start GPUI's drag. Commit the
                            // release position even when no subsequent move arrives.
                            state.update(cx, |state, cx| {
                                state.update_value_by_position(
                                    gpui::Axis::Horizontal,
                                    event.position,
                                    false,
                                    window,
                                    cx,
                                );
                                state.handle_release(cx);
                            });
                        }
                    });
                },
            )
            .absolute()
            .top_0()
            .left_0()
            .size_full(),
        )
    }
}

impl Render for Signals {
    fn render(&mut self, window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let (inputs, _) = self.state();
        for field in &mut self.fields {
            if let Some(row) = inputs
                .as_array()
                .into_iter()
                .flatten()
                .find(|row| row["id"] == field.id)
            {
                let current = row["value"].to_string();
                if current != field.observed
                    && !field.dragging.get()
                    && self.pending.as_ref().map(|(id, _)| id) != Some(&field.id)
                    && !self.queued.contains_key(&field.id)
                {
                    if field.input.read(cx).value().as_str() == field.observed {
                        field
                            .input
                            .update(cx, |input, cx| input.set_value(&current, window, cx));
                    }
                    field.slider.update(cx, |slider, cx| {
                        slider.set_value(row["value"].as_u64().unwrap() as f32, window, cx)
                    });
                    field.observed = current;
                }
            }
        }
        let mut rows = div().flex().flex_col().gap_4();
        if inputs.as_array().is_none() {
            rows = rows.child("当前运行未提供信号输入状态");
        }
        for (index, row) in inputs.as_array().into_iter().flatten().enumerate() {
            let id = row["id"].as_str().unwrap_or("").to_owned();
            let digital = row["type"] == "digital";
            let value = row["value"].clone();
            let label = row["label"].as_str().unwrap_or(&id).to_owned();
            let target = id.clone();
            let default = row["default"].to_string();
            let reset = Button::new(("reset-signal", index))
                .ghost()
                .accessibility_label(format!("恢复 {label} 默认值"))
                .icon(Icon::empty().path("reset"))
                .tooltip(format!(
                    "恢复默认：{}",
                    if digital {
                        if row["default"] == true {
                            "高电平".into()
                        } else {
                            "低电平".into()
                        }
                    } else {
                        format!("{} mV", row["default"])
                    }
                ))
                .disabled(
                    self.pending
                        .as_ref()
                        .is_some_and(|(pending, _)| pending == &id)
                        || row["overridden"] != true,
                )
                .on_click(cx.listener(move |view, _, window, cx| {
                    if let Some(field) = view.fields.iter().find(|field| field.id == target) {
                        field
                            .input
                            .update(cx, |input, cx| input.set_value(&default, window, cx));
                        field.slider.update(cx, |slider, cx| {
                            slider.set_value(default.parse::<f32>().unwrap(), window, cx)
                        });
                    }
                    view.apply(target.clone(), Value::Null, cx)
                }));
            let mut content = div()
                .flex()
                .flex_col()
                .gap_2()
                .pb_4()
                .border_b_1()
                .border_color(cx.theme().border)
                .child(
                    div()
                        .flex()
                        .items_center()
                        .justify_between()
                        .child(
                            div()
                                .min_w_0()
                                .flex_1()
                                .flex()
                                .flex_wrap()
                                .items_center()
                                .gap_2()
                                .child(label)
                                .when_some(
                                    row["description"].as_str().filter(|s| !s.is_empty()),
                                    |header, description| {
                                        header.child(caption(description.to_owned()))
                                    },
                                ),
                        )
                        .child(reset),
                );
            if digital {
                let target = id.clone();
                let pin = row["pin"].as_u64().unwrap_or(0);
                let endpoint = row["endpoint"].as_str()
                    .map(|text| text.strip_prefix("chip.").unwrap_or(text).to_owned())
                    .unwrap_or_else(|| format!("P{}{}", if pin < 32 { "A" } else { "B" }, pin % 32));
                content = content.child(
                    div()
                        .flex()
                        .items_center()
                        .justify_between()
                        .child(caption(endpoint))
                        .child(
                            Switch::new(("signal", index))
                                .checked(value == true)
                                .label(if value == true {
                                    "高电平"
                                } else {
                                    "低电平"
                                })
                                .disabled(
                                    self.pending
                                        .as_ref()
                                        .is_some_and(|(pending, _)| pending == &id),
                                )
                                .on_click(cx.listener(move |view, enabled: &bool, _, cx| {
                                    view.apply(target.clone(), json!(enabled), cx)
                                })),
                        ),
                );
            } else if let Some(field) = self.fields.iter().find(|field| field.id == id) {
                content = content
                    .child(
                        div()
                            .flex()
                            .items_center()
                            .justify_end()
                            .gap_2()
                            .child(div().w(px(160.)).child(NumberInput::new(&field.input)))
                            .child(caption("mV")),
                    )
                    .child(div().px_3().child(field.control()))
                    .child(
                        div()
                            .flex()
                            .px_3()
                            .justify_between()
                            .text_sm()
                            .text_color(cx.theme().muted_foreground)
                            .child("0")
                            .child(row["max"].to_string()),
                    );
            }
            rows = rows.child(content);
        }
        div()
            .id("signal-inputs")
            .flex_1()
            .min_h_0()
            .overflow_y_scrollbar()
            .child(rows)
            .child(
                div()
                    .py_3()
                    .text_sm()
                    .text_color(cx.theme().muted_foreground)
                    .child(self.message.clone()),
            )
    }
}
