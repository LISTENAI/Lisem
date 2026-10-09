//! Instance storage and runtime control shared by the CLI and desktop.
//! No window system or host audio device is required by this crate.
pub mod assets;
mod camera;
mod camera_capture;
mod camera_input;
pub mod catalog;
pub mod display;
pub mod identity;
pub mod manager;
pub mod paths;
pub mod process;
pub mod runtime;
mod shared;
pub mod storage;
mod transport;

pub mod install;
