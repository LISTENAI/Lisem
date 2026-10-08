use gpui::component::{
    ActiveTheme, TitleBar,
    button::{Button, ButtonVariants},
};
use gpui::{
    AnyWindowHandle, App, Bounds, Context, Render, TitlebarOptions, Window, WindowBounds,
    WindowOptions, div, img, prelude::*, px, size,
};

struct About;

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
                size(px(360.), px(360.)),
                cx,
            ))),
            is_resizable: false,
            window_min_size: Some(size(px(360.), px(360.))),
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
        |_, cx| cx.new(|_| About),
    )
    .expect("Could not open About window");
    handle
}
