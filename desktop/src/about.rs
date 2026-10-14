use gpui::component::{
    ActiveTheme, Disableable, TitleBar,
    button::{Button, ButtonVariants},
};
use gpui::{
    AnyWindowHandle, App, Bounds, Context, Render, Task, TitlebarOptions, Window, WindowBounds,
    WindowOptions, div, img, prelude::*, px, size,
};

use crate::updates::{self, CheckError, ReleaseStatus};

#[derive(Default)]
struct About {
    checking: bool,
    result: Option<Result<ReleaseStatus, CheckError>>,
    task: Option<Task<()>>,
}

impl About {
    fn check_updates(&mut self, cx: &mut Context<Self>) {
        if self.checking {
            return;
        }
        self.checking = true;
        self.result = None;
        cx.notify();
        self.task = Some(cx.spawn(async move |view, cx| {
            let result = cx
                .background_executor()
                .spawn(async { updates::check() })
                .await;
            let _ = view.update(cx, |view, cx| {
                view.checking = false;
                view.result = Some(result);
                cx.notify();
            });
        }));
    }
}

impl Render for About {
    fn render(&mut self, _: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        div()
            .size_full()
            .flex()
            .flex_col()
            .bg(cx.theme().background)
            .text_color(cx.theme().foreground)
            .child(TitleBar::new().h(px(52.)).bg(cx.theme().background))
            .child(
                div()
                    .flex_1()
                    .flex()
                    .flex_col()
                    .items_center()
                    .gap_3()
                    .px_6()
                    .pb_6()
                    .child(img("app-icon").size(px(96.)))
                    .child(
                        div()
                            .text_2xl()
                            .font_weight(gpui::FontWeight::SEMIBOLD)
                            .child("Lisem"),
                    )
                    .child(
                        div()
                            .text_sm()
                            .text_color(cx.theme().muted_foreground)
                            .child(format!("版本 {}", env!("CARGO_PKG_VERSION"))),
                    )
                    .child(div().text_sm().child("聆思芯片模拟器"))
                    .child(
                        Button::new("check-updates")
                            .label(if self.checking {
                                "正在检查…"
                            } else {
                                "检查更新"
                            })
                            .disabled(self.checking)
                            .on_click(cx.listener(|view, _, _, cx| view.check_updates(cx))),
                    )
                    .child(
                        div()
                            .h(px(64.))
                            .w_full()
                            .flex()
                            .flex_col()
                            .items_center()
                            .gap_1()
                            .child(
                                div()
                                    .text_sm()
                                    .text_color(cx.theme().muted_foreground)
                                    .child(match &self.result {
                                        Some(Ok(ReleaseStatus::Current)) => "已是最新版本".into(),
                                        Some(Ok(ReleaseStatus::Available { version, .. })) => {
                                            format!("发现新版本 {version}")
                                        }
                                        Some(Ok(ReleaseStatus::Unpublished)) => {
                                            "暂无已发布版本".into()
                                        }
                                        Some(Err(error)) => error.message(),
                                        None => String::new(),
                                    }),
                            )
                            .when_some(
                                self.result.as_ref().and_then(|result| match result {
                                    Ok(ReleaseStatus::Available { url, .. }) => Some(url.clone()),
                                    _ => None,
                                }),
                                |view, url| {
                                    view.child(
                                        Button::new("view-release")
                                            .label("查看新版本")
                                            .ghost()
                                            .on_click(move |_, _, cx| cx.open_url(&url)),
                                    )
                                },
                            ),
                    )
                    .child(
                        div()
                            .flex()
                            .gap_2()
                            .child(Button::new("project").label("项目主页").ghost().on_click(
                                |_, _, cx| cx.open_url("https://github.com/LISTENAI/Lisem"),
                            ))
                            .child(Button::new("license").label("许可证").ghost().on_click(
                                |_, _, cx| {
                                    cx.open_url(
                                        "https://github.com/LISTENAI/Lisem/blob/master/LICENSE",
                                    )
                                },
                            )),
                    )
                    .child(
                        div()
                            .text_xs()
                            .text_color(cx.theme().muted_foreground)
                            .child("© LISTENAI"),
                    ),
            )
    }
}

pub fn open(cx: &mut App) -> AnyWindowHandle {
    let (handle, _) = gpui::open_window(
        WindowOptions {
            window_bounds: Some(WindowBounds::Windowed(Bounds::centered(
                None,
                size(px(400.), px(480.)),
                cx,
            ))),
            is_resizable: false,
            window_min_size: Some(size(px(400.), px(480.))),
            titlebar: Some(TitlebarOptions {
                title: Some("关于 Lisem".into()),
                traffic_light_position: Some(gpui::point(px(16.), px(18.))),
                ..TitleBar::title_bar_options()
            }),
            app_id: Some("com.listenai.emulator".into()),
            app_owns_titlebar_drag: true,
            ..Default::default()
        },
        cx,
        |_, cx| cx.new(|_| About::default()),
    )
    .expect("Could not open About window");
    handle
}
