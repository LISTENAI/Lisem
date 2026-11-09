//! Rotary gestures enqueue detents; QEMU schedules and confirms every A/B edge.
use super::*;
use std::{collections::VecDeque, f32::consts::{PI, TAU}};

const STEP: f32 = TAU / 24.;

#[derive(Default)]
pub(super) struct Knob {
    run: String,
    angle: f32,
    remainder: f32,
    pub drag: Option<(f32, bool)>,
    pub queue: VecDeque<i32>,
    pending: bool,
}

fn angular_delta(previous: f32, next: f32) -> f32 {
    (next - previous + PI).rem_euclid(TAU) - PI
}

fn pointer_angle(bounds: Bounds<gpui::Pixels>, position: gpui::Point<gpui::Pixels>) -> Option<f32> {
    let x = f32::from(position.x - bounds.center().x);
    let y = f32::from(position.y - bounds.center().y);
    (x * x + y * y >= 144.).then(|| y.atan2(x))
}

impl Desktop {
    fn turn_knob(&mut self, id: &str, detents: i32, cx: &mut Context<Self>) {
        if !self.powered() || detents == 0 { return; }
        let knob = self.rotaries.get_mut(id).unwrap();
        if knob.queue.len() + detents.unsigned_abs() as usize > 64 {
            self.notice = "旋转输入过快，请稍后继续".into();
            return;
        }
        knob.queue.extend(std::iter::repeat_n(detents.signum(), detents.unsigned_abs() as usize));
        knob.angle = (knob.angle + STEP * detents as f32).rem_euclid(TAU);
        self.dispatch_knob(id, cx);
        cx.notify();
    }

    fn dispatch_knob(&mut self, id: &str, cx: &mut Context<Self>) {
        let mut params = self.params();
        let Some(knob) = self.rotaries.get_mut(id) else { return; };
        if knob.pending || knob.run != params["run"].as_str().unwrap_or_default() { return; }
        let Some(mut detents) = knob.queue.pop_front() else { return; };
        while detents.abs() < 32 && knob.queue.front() == Some(&detents.signum()) {
            detents += knob.queue.pop_front().unwrap();
        }
        knob.pending = true;
        let run = knob.run.clone();
        let id = id.to_owned();
        params["rotary"] = json!(id);
        params["action"] = json!("rotate");
        params["detents"] = json!(detents);
        params["edge_ms"] = json!(10);
        let receiver = self.backend.query("rotary", params.clone());
        let backend = self.backend.clone();
        cx.spawn(async move |view, cx| {
            let result: Result<(), String> = cx.background_executor().spawn(async move {
                let first = receiver.recv_timeout(Duration::from_secs(10))
                    .map_err(|_| "旋转输入未确认".to_owned())??;
                let sequence = first["sequence"].as_u64().ok_or("Missing rotary sequence")?;
                let mut query = json!({"id":params["id"],"run":params["run"],
                    "rotary":params["rotary"],"action":"status","sequence":sequence});
                let deadline = std::time::Instant::now() + Duration::from_secs(15);
                let mut status = first;
                loop {
                    if status["run"] != params["run"] || status["sequence"] != sequence {
                        return Err("旋转输入的设备运行已变化".into());
                    }
                    if status["status"] == "completed" { return Ok(()); }
                    if status["status"] != "running" {
                        return Err(format!("旋转输入未完成：{}", status["status"]));
                    }
                    if std::time::Instant::now() >= deadline {
                        query["action"] = json!("cancel");
                        let cleanup = backend.query("rotary", query).recv_timeout(Duration::from_secs(5));
                        return Err(format!("旋转输入确认超时；取消结果：{cleanup:?}"));
                    }
                    // Observe completion only; contact timing belongs to QEMU.
                    std::thread::sleep(Duration::from_millis(50));
                    status = backend.query("rotary", query.clone()).recv_timeout(Duration::from_secs(5))
                        .map_err(|_| "旋转输入状态查询超时".to_owned())??;
                }
            }).await;
            let _ = view.update(cx, |view, cx| {
                let Some(knob) = view.rotaries.get_mut(&id).filter(|knob| knob.run == run) else { return; };
                knob.pending = false;
                match result {
                    Ok(()) => view.dispatch_knob(&id, cx),
                    Err(error) => { knob.queue.clear(); view.notice = error; }
                }
                cx.notify();
            });
        }).detach();
    }

    pub(super) fn rotary_control(&mut self, descriptor: &Value, on: bool, cx: &mut Context<Self>) -> Div {
        let id = descriptor["id"].as_str().unwrap_or_default().to_owned();
        let run = self.params()["run"].as_str().unwrap_or_default().to_owned();
        let knob = self.rotaries.entry(id.clone()).or_default();
        if knob.run != run { *knob = Knob { run, ..Default::default() }; }
        let angle = knob.angle;
        let bounds = Rc::new(RefCell::new(None));
        let paint_bounds = bounds.clone();
        let down_bounds = bounds.clone();
        let down_id = id.clone();
        let move_id = id.clone();
        let up_id = id.clone();
        let out_id = id.clone();
        let wheel_id = id.clone();
        let dot_color = if on { cx.theme().primary } else { cx.theme().muted_foreground };
        let dial = div().id(SharedString::from(format!("rotary-{id}")))
            .relative().size(px(96.)).rounded_full().border_2()
            .border_color(if on { cx.theme().border } else { cx.theme().muted })
            .bg(cx.theme().secondary)
            .when(on, |e| e.cursor_pointer())
            .child(gpui::canvas(move |b, _, _| { *paint_bounds.borrow_mut() = Some(b); }, |_, _, _, _| {})
                .absolute().size_full())
            .child(div().absolute().left(px(43. + 32. * angle.sin())).top(px(43. - 32. * angle.cos()))
                .size(px(10.)).rounded_full().bg(dot_color))
            .child(div().absolute().inset_0().flex().items_center().justify_center()
                .text_sm().text_color(cx.theme().muted_foreground).child("−     +"))
            .on_mouse_down(MouseButton::Left, cx.listener(move |view, event: &gpui::MouseDownEvent, _, cx| {
                if !view.powered() { return; }
                if let Some(b) = *down_bounds.borrow() {
                    if let Some(angle) = pointer_angle(b, event.position) {
                        let knob = view.rotaries.get_mut(&down_id).unwrap();
                        knob.remainder = 0.;
                        knob.drag = Some((angle, false));
                        cx.notify();
                    }
                }
            }))
            .on_mouse_move(cx.listener(move |view, event: &gpui::MouseMoveEvent, _, cx| {
                if !view.powered() { return; }
                let knob = view.rotaries.get_mut(&move_id).unwrap();
                if event.pressed_button != Some(MouseButton::Left) { knob.drag = None; return; }
                let Some((previous, moved)) = knob.drag else { return; };
                let Some(b) = *bounds.borrow() else { return; };
                let Some(angle) = pointer_angle(b, event.position) else { return; };
                let delta = angular_delta(previous, angle);
                knob.remainder += delta;
                let steps = (knob.remainder / STEP).trunc() as i32;
                knob.remainder -= STEP * steps as f32;
                knob.drag = Some((angle, moved || steps != 0));
                view.turn_knob(&move_id, steps, cx);
            }))
            .on_mouse_up(MouseButton::Left, cx.listener(move |view, _, _, cx| {
                let Some(knob) = view.rotaries.get_mut(&up_id) else { return; };
                if let Some((angle, false)) = knob.drag.take() {
                    view.turn_knob(&up_id, if angle.cos() < 0. { -1 } else { 1 }, cx);
                } else { knob.drag = None; }
            }))
            .on_mouse_up_out(MouseButton::Left, cx.listener(move |view, _, _, _| {
                if let Some(knob) = view.rotaries.get_mut(&out_id) { knob.drag = None; }
            }))
            .on_scroll_wheel(cx.listener(move |view, event: &gpui::ScrollWheelEvent, _, cx| {
                if !view.powered() { return; }
                let delta = event.delta.pixel_delta(px(20.));
                let knob = view.rotaries.get_mut(&wheel_id).unwrap();
                knob.remainder += -f32::from(delta.y) * STEP / 20.;
                let steps = (knob.remainder / STEP).trunc() as i32;
                knob.remainder -= STEP * steps as f32;
                view.turn_knob(&wheel_id, steps.clamp(-32, 32), cx);
                cx.stop_propagation();
            }));
        div().flex().flex_col().items_center().gap_2().child(dial)
            .child(descriptor["label"].as_str().unwrap_or(&id).to_owned())
            .child(div().text_xs().text_color(cx.theme().muted_foreground).child("拖动或滚动 · 两侧单步"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn drag_crosses_angle_wrap_in_both_directions() {
        assert!((angular_delta(PI - 0.1, -PI + 0.1) - 0.2).abs() < 0.0001);
        assert!((angular_delta(-PI + 0.1, PI - 0.1) + 0.2).abs() < 0.0001);
        assert_eq!(angular_delta(0.5, 0.5), 0.);
    }
}
