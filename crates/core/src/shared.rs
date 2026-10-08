//! Named, kernel-backed memory shared by the worker, QEMU and native clients.
//! No filesystem-backed mapping is used for normal audio or display traffic.
use anyhow::{Result, ensure};
#[cfg(unix)]
use std::ffi::CString;
use std::{
    ops::Deref,
    path::{Path, PathBuf},
};

pub struct Mapping {
    pointer: *mut u8,
    length: usize,
}
// Shared accesses use the transport's atomic ownership protocol.
unsafe impl Send for Mapping {}
unsafe impl Sync for Mapping {}
impl Mapping {
    pub fn open(name: &str, length: usize) -> Result<Self> {
        ensure!(valid_name(name), "Invalid shared memory name");
        ensure!(
            length > 0 && length <= 64 * 1024 * 1024,
            "Invalid shared memory size"
        );
        #[cfg(unix)]
        let pointer = {
            let name = CString::new(format!("/{name}"))?;
            let fd = unsafe { libc::shm_open(name.as_ptr(), libc::O_RDWR, 0) };
            ensure!(
                fd >= 0,
                "Cannot open shared memory: {}",
                std::io::Error::last_os_error()
            );
            let mut info = std::mem::MaybeUninit::<libc::stat>::uninit();
            let result = unsafe { libc::fstat(fd, info.as_mut_ptr()) };
            let valid = result == 0 && unsafe { info.assume_init() }.st_size >= length as i64;
            if !valid {
                unsafe {
                    libc::close(fd);
                }
                anyhow::bail!("Shared memory is too small");
            }
            let pointer = unsafe {
                libc::mmap(
                    std::ptr::null_mut(),
                    length,
                    libc::PROT_READ | libc::PROT_WRITE,
                    libc::MAP_SHARED,
                    fd,
                    0,
                )
            };
            unsafe {
                libc::close(fd);
            }
            ensure!(
                pointer != libc::MAP_FAILED,
                "Cannot map shared memory: {}",
                std::io::Error::last_os_error()
            );
            pointer.cast()
        };
        #[cfg(windows)]
        let pointer = {
            let name: Vec<u16> = format!("Local\\{name}\0").encode_utf16().collect();
            let handle = unsafe { windows::OpenFileMappingW(0x000f001f, 0, name.as_ptr()) };
            ensure!(
                !handle.is_null(),
                "Cannot open shared memory: {}",
                std::io::Error::last_os_error()
            );
            let pointer = unsafe { windows::MapViewOfFile(handle, 0x000f001f, 0, 0, length) };
            unsafe {
                windows::CloseHandle(handle);
            }
            ensure!(
                !pointer.is_null(),
                "Cannot map shared memory: {}",
                std::io::Error::last_os_error()
            );
            pointer.cast()
        };
        Ok(Self { pointer, length })
    }
}
impl Deref for Mapping {
    type Target = [u8];
    fn deref(&self) -> &[u8] {
        unsafe { std::slice::from_raw_parts(self.pointer, self.length) }
    }
}
impl Drop for Mapping {
    fn drop(&mut self) {
        #[cfg(unix)]
        unsafe {
            libc::munmap(self.pointer.cast(), self.length);
        }
        #[cfg(windows)]
        unsafe {
            windows::UnmapViewOfFile(self.pointer.cast());
        }
    }
}

fn valid_name(name: &str) -> bool {
    name.len() == 28 && name.starts_with("lsm-") && name[4..].bytes().all(|b| b.is_ascii_hexdigit())
}
fn unlink(name: &str) {
    #[cfg(unix)]
    if valid_name(name) {
        let name = CString::new(format!("/{name}")).unwrap();
        unsafe {
            libc::shm_unlink(name.as_ptr());
        }
    }
    #[cfg(windows)]
    let _ = name; // Windows reclaims the section when its last mapping closes.
}

/// The storage lease must be held before recovering an interrupted owner's
/// names. A live QEMU inherits that lease, so its mappings cannot be removed.
pub struct Regions {
    registry: PathBuf,
    names: Vec<String>,
}
impl Regions {
    pub fn new(instance: &Path) -> Result<Self> {
        let registry = instance.join("ipc.json");
        if registry.exists() {
            let previous: Vec<String> = serde_json::from_slice(&std::fs::read(&registry)?)?;
            for name in previous {
                unlink(&name);
            }
            std::fs::remove_file(&registry)?;
        }
        Ok(Self {
            registry,
            names: Vec::new(),
        })
    }
    pub fn allocate(&mut self) -> Result<String> {
        let name = format!("lsm-{}", &uuid::Uuid::new_v4().simple().to_string()[..24]);
        self.names.push(name.clone());
        crate::storage::write_json(&self.registry, &serde_json::json!(self.names))?;
        Ok(format!("shm:{name}"))
    }
}
impl Drop for Regions {
    fn drop(&mut self) {
        for name in &self.names {
            unlink(name);
        }
        let _ = std::fs::remove_file(&self.registry);
    }
}

#[cfg(windows)]
mod windows {
    use std::ffi::c_void;
    #[link(name = "kernel32")]
    unsafe extern "system" {
        pub fn OpenFileMappingW(access: u32, inherit: i32, name: *const u16) -> *mut c_void;
        pub fn MapViewOfFile(
            handle: *mut c_void,
            access: u32,
            high: u32,
            low: u32,
            bytes: usize,
        ) -> *mut c_void;
        pub fn UnmapViewOfFile(address: *const c_void) -> i32;
        pub fn CloseHandle(handle: *mut c_void) -> i32;
    }
}
