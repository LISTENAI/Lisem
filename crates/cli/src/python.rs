//! Run the user's interpreter with this executable's paired SDK.
use anyhow::{Context, Result, bail, ensure};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    env,
    ffi::{OsStr, OsString},
    fs,
    path::{Path, PathBuf},
    process::Command,
};

pub fn sdk_info() -> Result<Value> {
    let executable = env::current_exe()?.canonicalize()?;
    let directory = executable.parent().context("Executable has no directory")?;
    let candidates = [
        directory.join("runtime/python"),
        directory.join("../Resources/runtime/python"),
    ];
    let root = candidates
        .iter()
        .find(|p| p.join("lisem-sdk.json").is_file())
        .context("Bundled Python SDK missing; use a complete Lisem application package")?
        .canonicalize()?;
    let metadata: Value = serde_json::from_slice(&fs::read(root.join("lisem-sdk.json"))?)?;
    let build = lisem_core::identity::build();
    ensure!(
        metadata["format"] == 1 && metadata["build"] == build,
        "Python SDK and native build differ; use a complete matching application package"
    );
    ensure!(
        metadata["version"] == build["version"],
        "Python SDK product version differs"
    );
    let cli = metadata["executable"]
        .as_str()
        .context("SDK executable path missing")?;
    ensure!(
        root.join(cli).canonicalize()? == executable,
        "Python SDK belongs to another executable"
    );
    let mut actual = Vec::new();
    collect(&root, &root, &mut actual)?;
    actual.sort();
    ensure!(
        actual == ["lisem-sdk.json", "lisem-sdk.zip"],
        "Bundled SDK file set changed; restore the application package"
    );
    let digest = format!(
        "{:x}",
        Sha256::digest(fs::read(root.join("lisem-sdk.zip"))?)
    );
    ensure!(
        metadata["archive"] == "lisem-sdk.zip"
            && metadata["sha256"].as_str() == Some(digest.as_str()),
        "Bundled SDK archive checksum mismatch"
    );

    Ok(
        serde_json::json!({"path":root.join("lisem-sdk.zip"),"version":metadata["version"],"build":build,
        "executable":executable,"manifest_sha256":format!("{:x}",Sha256::digest(fs::read(root.join("lisem-sdk.json"))?))}),
    )
}

fn collect(root: &Path, directory: &Path, names: &mut Vec<String>) -> Result<()> {
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        let path = entry.path();
        let kind = entry.file_type()?;
        ensure!(!kind.is_symlink(), "Bundled SDK contains a symbolic link");
        if kind.is_dir() {
            collect(root, &path, names)?;
        } else if kind.is_file() {
            names.push(
                path.strip_prefix(root)?
                    .to_str()
                    .context("SDK path is not UTF-8")?
                    .replace('\\', "/"),
            );
        } else {
            bail!("Unexpected bundled SDK file type");
        }
    }
    Ok(())
}

fn find(program: &OsStr) -> Option<PathBuf> {
    let path = Path::new(program);
    if path.components().count() > 1 || path.is_absolute() {
        return path.is_file().then(|| path.to_owned());
    }
    env::split_paths(&env::var_os("PATH").unwrap_or_default()).find_map(|directory| {
        let path = directory.join(program);
        if path.is_file() {
            Some(path)
        } else if cfg!(windows)
            && path.extension().is_none()
            && path.with_extension("exe").is_file()
        {
            Some(path.with_extension("exe"))
        } else {
            None
        }
    })
}

fn interpreter(explicit: Option<PathBuf>) -> Result<PathBuf> {
    if let Some(path) = explicit {
        return find(path.as_os_str()).context("Python interpreter not found");
    }
    for name in ["VIRTUAL_ENV", "CONDA_PREFIX"] {
        if let Some(root) = env::var_os(name).filter(|v| !v.is_empty()) {
            let directory = PathBuf::from(root).join(if cfg!(windows) { "Scripts" } else { "bin" });
            // Keep the venv executable spelling: resolving its symlink would select the base environment.
            for name in if cfg!(windows) {
                ["python.exe", "python3.exe"]
            } else {
                ["python", "python3"]
            } {
                let candidate = directory.join(name);
                if candidate.is_file() {
                    return Ok(candidate);
                }
            }
            // Conda's Windows interpreter lives at the environment root.
            if cfg!(windows) && name == "CONDA_PREFIX" {
                let candidate = directory.parent().unwrap().join("python.exe");
                if candidate.is_file() {
                    return Ok(candidate);
                }
            }
            bail!("Active {name} has no Python interpreter; select one with --interpreter");
        }
    }
    let names = if cfg!(windows) {
        ["python", "python3"]
    } else {
        ["python3", "python"]
    };
    names
        .iter()
        .find_map(|name| find(OsStr::new(name)))
        .context("Python 3.10+ not found; install Python or select --interpreter")
}

fn check_options(args: &[OsString]) -> Result<()> {
    let mut value = false;
    for argument in args {
        if value {
            value = false;
            continue;
        }
        let Some(argument) = argument.to_str() else {
            break;
        };
        if argument == "--" || argument == "-" || !argument.starts_with('-') {
            break;
        }
        if argument == "--check-hash-based-pycs" {
            value = true;
            continue;
        }
        if argument.starts_with("--") {
            continue;
        }
        for (offset, option) in argument[1..].char_indices() {
            match option {
                'E' | 'I' | 'S' => bail!(
                    "Python -{option} disables bundled SDK initialization; use an external interpreter with the queried SDK path for custom isolation"
                ),
                'c' | 'm' => return Ok(()),
                'W' | 'X' => {
                    value = offset + 2 == argument.len();
                    break;
                }
                _ => {}
            }
        }
    }
    Ok(())
}

pub fn run(explicit: Option<PathBuf>, args: Vec<OsString>) -> Result<i32> {
    check_options(&args)?;
    let sdk = sdk_info()?;
    let mut command = Command::new(interpreter(explicit)?);
    let path = PathBuf::from(sdk["path"].as_str().unwrap());
    let mut paths = vec![path.clone()];
    if let Some(previous) = env::var_os("PYTHONPATH") {
        paths.extend(env::split_paths(&previous));
    }
    command
        .args(args)
        .env("PYTHONPATH", env::join_paths(paths)?)
        .env("LISEM_PYTHON_SDK", &path)
        .env("LISEM_EXECUTABLE", sdk["executable"].as_str().unwrap())
        .env_remove("LISEM_ROOT");
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        Err(command.exec()).context("Cannot execute Python")
    }
    #[cfg(windows)]
    {
        windows::run(command)
    }
}

#[cfg(windows)]
mod windows {
    use super::*;
    use std::{ffi::c_void, os::windows::io::AsRawHandle};
    #[repr(C)]
    #[derive(Default)]
    struct Limits {
        process_time: i64,
        job_time: i64,
        flags: u32,
        min_working: usize,
        max_working: usize,
        active: u32,
        affinity: usize,
        priority: u32,
        scheduling: u32,
    }
    #[repr(C)]
    #[derive(Default)]
    struct Extended {
        basic: Limits,
        io: [u64; 6],
        process_memory: usize,
        job_memory: usize,
        peak_process: usize,
        peak_job: usize,
    }
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn CreateJobObjectW(attributes: *const c_void, name: *const u16) -> *mut c_void;
        fn SetInformationJobObject(
            job: *mut c_void,
            class: u32,
            information: *const c_void,
            size: u32,
        ) -> i32;
        fn AssignProcessToJobObject(job: *mut c_void, process: *mut c_void) -> i32;
        fn CloseHandle(handle: *mut c_void) -> i32;
    }
    struct Job(*mut c_void);
    impl Drop for Job {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
    pub fn run(mut command: Command) -> Result<i32> {
        // The child remains in this console and gets Ctrl+C itself; keep only the waiting parent alive.
        ctrlc::set_handler(|| {})?;
        let job = Job(unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) });
        ensure!(
            !job.0.is_null(),
            "Cannot create Python process job: {}",
            std::io::Error::last_os_error()
        );
        // Contain the direct interpreter only. Its children retain normal SDK/CLI ownership
        // and background lifetimes, rather than being killed as an undifferentiated tree.
        let limits = Extended {
            basic: Limits {
                flags: 0x2000 | 0x1000,
                ..Default::default()
            },
            ..Default::default()
        };
        ensure!(
            unsafe {
                SetInformationJobObject(
                    job.0,
                    9,
                    &limits as *const _ as *const c_void,
                    std::mem::size_of::<Extended>() as u32,
                )
            } != 0,
            "Cannot configure Python process job: {}",
            std::io::Error::last_os_error()
        );
        // Command inherits the console and stdio. Internal CREATE_NO_WINDOW helpers are unsuitable here.
        let mut child = command.spawn().context("Cannot start Python")?;
        if unsafe { AssignProcessToJobObject(job.0, child.as_raw_handle()) } == 0 {
            let error = std::io::Error::last_os_error();
            let _ = child.kill();
            let _ = child.wait();
            bail!("Cannot contain Python process: {error}");
        }
        Ok(child.wait()?.code().unwrap_or(1))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn options_end_at_the_python_interface() {
        for args in [
            vec!["-c", "print('ok')", "-I"],
            vec!["script.py", "-S"],
            vec!["-X", "dev", "-m", "pytest", "-E"],
            vec!["--", "-I"],
        ] {
            assert!(check_options(&args.iter().map(OsString::from).collect::<Vec<_>>()).is_ok());
        }
        for args in [
            vec!["-I"],
            vec!["-uS", "script.py"],
            vec!["-X", "dev", "-E"],
            vec!["-Wignore", "-S"],
        ] {
            assert!(check_options(&args.iter().map(OsString::from).collect::<Vec<_>>()).is_err());
        }
    }
}
