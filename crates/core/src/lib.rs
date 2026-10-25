//! Instance storage and runtime control shared by the CLI and desktop.
//! No window system or host audio device is required by this crate.
pub mod assets;
pub mod board;
mod camera;
mod camera_capture;
mod camera_input;
pub mod catalog;
pub mod display;
pub mod identity;
pub mod manager;
pub mod rpc;
pub mod paths;
pub mod process;
pub mod runtime;
mod shared;
pub mod storage;
mod transport;
mod uart_capture;
mod wave_input;
mod sd_card;

pub mod install;

pub mod wifi_environment;

pub mod signals;
pub mod signal_trace;
