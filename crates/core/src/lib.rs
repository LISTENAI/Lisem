//! Instance storage and runtime control shared by the CLI and desktop.
//! No window system or host audio device is required by this crate.
pub mod assets;
pub mod catalog;
pub mod display;
pub mod manager;
pub mod paths;
pub mod process;
pub mod runtime;
pub mod storage;
mod transport;
