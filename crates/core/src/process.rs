//! Internal helpers are controlled over pipes/sockets, never a new console.
use std::{ffi::OsStr, process::Command};

pub fn command(program: impl AsRef<OsStr>) -> Command {
    let mut command = Command::new(program);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
    #[cfg(not(windows))]
    let _ = &mut command;
    command
}

/// Keep a launcher-provided pipe from leaking into background descendants.
/// Command creates its own inheritable duplicates for explicitly selected stdio.
pub fn init() -> std::io::Result<()> {
    #[cfg(windows)]
    {
        use std::ffi::c_void;
        #[link(name = "kernel32")]
        unsafe extern "system" {
            fn GetStdHandle(id: u32) -> *mut c_void;
            fn SetHandleInformation(handle: *mut c_void, mask: u32, flags: u32) -> i32;
        }
        for id in [-10_i32, -11, -12] {
            let handle = unsafe { GetStdHandle(id as u32) };
            if !handle.is_null()
                && handle as isize != -1
                && unsafe { SetHandleInformation(handle, 1, 0) } == 0
            {
                return Err(std::io::Error::last_os_error());
            }
        }
    }
    Ok(())
}
