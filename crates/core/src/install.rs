//! Per-user command discovery for a bundled CLI.
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
};

pub struct Installation {
    pub command: PathBuf,
    pub path_ready: bool,
}

pub fn bundled_cli() -> Result<PathBuf> {
    let path = std::env::var_os("LISEM_CLI").map(PathBuf::from).unwrap_or(
        std::env::current_exe()?.with_file_name(if cfg!(windows) { "lisem.exe" } else { "lisem" }),
    );
    ensure!(path.is_file(), "Bundled CLI is missing: {}", path.display());
    Ok(path.canonicalize()?)
}

pub fn install() -> Result<Installation> {
    let target = bundled_cli()?;
    let data = crate::paths::data_dir()?;
    fs::create_dir_all(&data)?;
    let record = data.join("cli-install.json");
    let previous = crate::storage::read_json(&record).unwrap_or(Value::Null);
    #[cfg(unix)]
    let (command, path_ready, state) = {
        let directory = if cfg!(target_os = "macos") {
            PathBuf::from("/usr/local/bin")
        } else {
            PathBuf::from(std::env::var_os("HOME").context("HOME is missing")?).join(".local/bin")
        };
        let destination = directory.join("lisem");
        let old = (previous["command"].as_str().map(Path::new) == Some(&destination))
            .then(|| previous["target"].as_str().map(Path::new))
            .flatten();
        let result = link(&target, &destination, old);
        #[cfg(target_os = "macos")]
        let result = match result {
            Err(error)
                if error
                    .downcast_ref::<std::io::Error>()
                    .is_some_and(|e| e.kind() == std::io::ErrorKind::PermissionDenied) =>
            {
                elevated_link(&target, &destination, old)
            }
            result => result,
        };
        result?;
        let ready = std::env::var_os("PATH")
            .is_some_and(|p| std::env::split_paths(&p).any(|p| p == directory));
        let state = json!({"command":destination, "target":target});
        (destination, ready, state)
    };
    #[cfg(windows)]
    let (command, path_ready, state) = {
        let directory = target.parent().context("CLI directory is missing")?;
        install_windows_path(directory, previous["directory"].as_str().map(Path::new))?;
        (
            target.clone(),
            true,
            json!({"directory":directory, "target":target}),
        )
    };
    crate::storage::write_json(&record, &state)?;
    Ok(Installation {
        command,
        path_ready,
    })
}

#[cfg(unix)]
fn link(target: &Path, destination: &Path, previous: Option<&Path>) -> Result<()> {
    use std::os::unix::fs::symlink;
    fs::create_dir_all(destination.parent().context("CLI directory missing")?)?;
    if let Ok(current) = fs::read_link(destination) {
        if current == target {
            return Ok(());
        }
        ensure!(
            Some(current.as_path()) == previous,
            "An unrelated command already exists: {}",
            destination.display()
        );
        fs::remove_file(destination)?;
        if let Err(error) = symlink(target, destination) {
            let _ = symlink(current, destination);
            return Err(error.into());
        }
    } else {
        symlink(target, destination)
            .with_context(|| format!("Cannot install command: {}", destination.display()))?;
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn elevated_link(target: &Path, destination: &Path, previous: Option<&Path>) -> Result<()> {
    fn quote(path: &Path) -> Result<String> {
        Ok(format!(
            "'{}'",
            path.to_str()
                .context("Invalid CLI path")?
                .replace('\'', "'\\''")
        ))
    }
    let dest = quote(destination)?;
    let target = quote(target)?;
    let owned = previous
        .map(quote)
        .transpose()?
        .unwrap_or_else(|| "''".into());
    let script = format!(
        "/bin/mkdir -p /usr/local/bin; if [ -L {dest} ]; then current=$(/usr/bin/readlink {dest}); if [ \"$current\" = {target} ]; then exit 0; fi; [ \"$current\" = {owned} ] || exit 1; /bin/rm {dest} || exit 1; fi; /bin/ln -s {target} {dest}"
    );
    let apple = format!(
        "do shell script \"{}\" with administrator privileges",
        script.replace('\\', "\\\\").replace('"', "\\\"")
    );
    let output = crate::process::command("/usr/bin/osascript")
        .args(["-e", &apple])
        .output()?;
    ensure!(
        output.status.success(),
        "CLI installation was cancelled or denied: {}",
        String::from_utf8_lossy(&output.stderr).trim()
    );
    Ok(())
}

#[cfg(windows)]
fn install_windows_path(directory: &Path, previous: Option<&Path>) -> Result<()> {
    fn quote(path: &Path) -> Result<String> {
        Ok(format!(
            "'{}'",
            path.to_str()
                .context("Invalid CLI path")?
                .replace('\'', "''")
        ))
    }
    let dir = quote(directory)?;
    let old = previous
        .map(quote)
        .transpose()?
        .unwrap_or_else(|| "''".into());
    let script = format!(
        r#"
$ErrorActionPreference = 'Stop'
$parts = @([Environment]::GetEnvironmentVariable('Path', 'User') -split ';' | Where-Object {{ $_ -and $_ -ine {old} -and $_ -ine {dir} }})
[Environment]::SetEnvironmentVariable('Path', (($parts + {dir}) -join ';'), 'User')
Add-Type 'using System; using System.Runtime.InteropServices; public class LisemEnvironment {{ [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr SendMessageTimeout(IntPtr h, uint m, UIntPtr w, string l, uint f, uint t, out UIntPtr r); }}'
[UIntPtr]$result = [UIntPtr]::Zero
[LisemEnvironment]::SendMessageTimeout([IntPtr]0xffff, 0x1a, [UIntPtr]::Zero, 'Environment', 2, 3000, [ref]$result) | Out-Null
"#
    );
    let executable =
        PathBuf::from(std::env::var_os("SystemRoot").context("SystemRoot is missing")?)
            .join("System32/WindowsPowerShell/v1.0/powershell.exe");
    let output = crate::process::command(executable)
        .args(["-NoProfile", "-NonInteractive", "-Command", &script])
        .output()?;
    ensure!(
        output.status.success(),
        "Cannot update user PATH: {}",
        String::from_utf8_lossy(&output.stderr).trim()
    );
    Ok(())
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    #[test]
    fn owned_links_can_move_but_unrelated_commands_are_preserved() {
        let temp = tempfile::tempdir().unwrap();
        let first = temp.path().join("first/lisem");
        let second = temp.path().join("second/lisem");
        let command = temp.path().join("bin/lisem");
        link(&first, &command, None).unwrap();
        link(&first, &command, None).unwrap();
        assert!(link(&second, &command, None).is_err());
        link(&second, &command, Some(&first)).unwrap();
        assert_eq!(fs::read_link(&command).unwrap(), second);
        fs::remove_file(&command).unwrap();
        fs::write(&command, b"unrelated").unwrap();
        assert!(link(&first, &command, Some(&second)).is_err());
        assert_eq!(fs::read(&command).unwrap(), b"unrelated");
    }
}
