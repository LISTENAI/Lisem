//! Platform window chrome; application controls share its draggable header.

#[cfg(not(target_os = "linux"))]
pub use gpui::component::TitleBar;
#[cfg(target_os = "linux")]
pub use linux::TitleBar;

/// Space owned by native window controls and the draggable header's leading inset.
pub fn header_reserved_width(window: &gpui::Window) -> f32 {
    #[cfg(target_os = "macos")]
    {
        let _ = window;
        80.
    }
    #[cfg(target_os = "windows")]
    {
        let _ = window;
        12. + 3. * 34.
    }
    #[cfg(target_os = "linux")]
    {
        let controls = window.window_controls();
        12. + 24.
            + 30.
            + if controls.minimize { 38. } else { 0. }
            + if controls.maximize { 38. } else { 0. }
    }
}

pub fn window_options(options: gpui::WindowOptions) -> gpui::WindowOptions {
    #[cfg(target_os = "linux")]
    {
        gpui::WindowOptions {
            window_decorations: Some(gpui::WindowDecorations::Client),
            window_background: gpui::WindowBackgroundAppearance::Transparent,
            ..options
        }
    }
    #[cfg(not(target_os = "linux"))]
    options
}

#[cfg(target_os = "linux")]
mod linux {
    use gpui::component::{
        ActiveTheme, Icon, IconName, InteractiveElementExt, Sizable, StyledExt,
        button::{Button, ButtonVariants},
    };
    use gpui::{
        AnyElement, App, ClickEvent, IntoElement, MouseButton, ParentElement, Pixels, Point,
        RenderOnce, StyleRefinement, Styled, TitlebarOptions, Window, div, prelude::*, px,
    };
    use std::rc::Rc;

    #[derive(IntoElement)]
    pub struct TitleBar {
        style: StyleRefinement,
        children: Vec<AnyElement>,
        on_close: Option<Rc<dyn Fn(&ClickEvent, &mut Window, &mut App)>>,
    }

    impl TitleBar {
        pub fn new() -> Self {
            Self {
                style: StyleRefinement::default(),
                children: Vec::new(),
                on_close: None,
            }
        }

        pub fn on_close_window(
            mut self,
            close: impl Fn(&ClickEvent, &mut Window, &mut App) + 'static,
        ) -> Self {
            self.on_close = Some(Rc::new(close));
            self
        }

        pub fn title_bar_options() -> TitlebarOptions {
            gpui::component::TitleBar::title_bar_options()
        }
    }

    impl Styled for TitleBar {
        fn style(&mut self) -> &mut StyleRefinement {
            &mut self.style
        }
    }

    impl ParentElement for TitleBar {
        fn extend(&mut self, children: impl IntoIterator<Item = AnyElement>) {
            self.children.extend(children);
        }
    }

    fn control(id: &'static str, label: &'static str, icon: IconName, cx: &App) -> Button {
        Button::new(id)
            .ghost()
            .icon(Icon::new(icon).small())
            .w(px(30.))
            .h(px(30.))
            .rounded_full()
            .bg(cx.theme().secondary)
            .accessibility_label(label)
            .tooltip(label)
            .on_mouse_down(MouseButton::Left, |_, _, cx| cx.stop_propagation())
    }

    impl RenderOnce for TitleBar {
        fn render(self, window: &mut Window, cx: &mut App) -> impl IntoElement {
            let drag = window.use_state(cx, |_, _| None::<Point<Pixels>>);
            let controls = window.window_controls();
            let close = self.on_close;
            let client = matches!(
                window.window_decorations(),
                gpui::Decorations::Client { .. }
            );
            div()
                .id("lisem-window-header")
                .flex()
                .items_center()
                .flex_shrink_0()
                .w_full()
                .h(px(52.))
                .pl_3()
                .bg(cx.theme().background)
                .refine_style(&self.style)
                .on_mouse_down(
                    MouseButton::Left,
                    window.listener_for(&drag, |drag, event: &gpui::MouseDownEvent, window, _| {
                        *drag = (!window.default_prevented()).then_some(event.position);
                    }),
                )
                .on_mouse_down_out(window.listener_for(&drag, |drag, _, _, _| *drag = None))
                .on_mouse_up(
                    MouseButton::Left,
                    window.listener_for(&drag, |drag, _, _, _| *drag = None),
                )
                .on_mouse_move(window.listener_for(
                    &drag,
                    |drag, event: &gpui::MouseMoveEvent, window, _| {
                        if let Some(start) = *drag {
                            let distance = event.position - start;
                            if distance.x.abs() + distance.y.abs() >= px(4.) {
                                *drag = None;
                                window.start_window_move();
                            }
                        }
                    },
                ))
                .on_double_click(|_, window, _| window.zoom_window())
                .on_mouse_down(MouseButton::Right, |event, window, cx| {
                    window.show_window_menu(event.position);
                    cx.stop_propagation();
                })
                .child(
                    div()
                        .flex()
                        .items_center()
                        .flex_1()
                        .min_w_0()
                        .h_full()
                        .children(self.children),
                )
                .when(client, |header| {
                    header.child(
                        div()
                            .flex()
                            .items_center()
                            .flex_shrink_0()
                            .gap_2()
                            .px_3()
                            .when(controls.minimize, |row| {
                                row.child(
                                    control(
                                        "window-minimize",
                                        "最小化",
                                        IconName::WindowMinimize,
                                        cx,
                                    )
                                    .on_click(|_, window, _| window.minimize_window()),
                                )
                            })
                            .when(controls.maximize, |row| {
                                row.child(
                                    control(
                                        "window-maximize",
                                        if window.is_maximized() {
                                            "还原"
                                        } else {
                                            "最大化"
                                        },
                                        if window.is_maximized() {
                                            IconName::WindowRestore
                                        } else {
                                            IconName::WindowMaximize
                                        },
                                        cx,
                                    )
                                    .on_click(|_, window, _| window.zoom_window()),
                                )
                            })
                            .child(
                                control("window-close", "关闭", IconName::WindowClose, cx)
                                    .on_click(move |event, window, cx| {
                                        if let Some(close) = &close {
                                            close(event, window, cx);
                                        } else {
                                            window.remove_window();
                                        }
                                    }),
                            ),
                    )
                })
        }
    }
}
