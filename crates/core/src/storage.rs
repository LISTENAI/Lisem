//! Persistent instance storage. Board/chip descriptions determine the layout;
//! writing Flash never changes OTP, UID, or immutable chip ROM assets.
use anyhow::{Context, Result, ensure};
use md5::Md5;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct Layout {
    pub chip: String,
    pub board: String,
    pub flash_bytes: usize,
    pub otp_bytes: usize,
    pub uid_offset: usize,
    pub uid_bytes: usize,
}

pub struct Lease {
    pub file: File,
}
impl Lease {
    pub fn acquire(path: &Path) -> Result<Self> {
        let mut options = OpenOptions::new();
        options.create(true).truncate(false).read(true).write(true);
        #[cfg(windows)]
        {
            use std::os::windows::fs::OpenOptionsExt;
            // A share-denying open survives in duplicated/inherited handles;
            // Windows byte-range locks instead belong to the locking process.
            options.share_mode(0);
        }
        let file = options.open(path).map_err(|error| {
            if cfg!(windows) && matches!(error.raw_os_error(), Some(32 | 33)) {
                anyhow::anyhow!("Resource is in use: {}", path.display())
            } else {
                anyhow::anyhow!("Cannot acquire resource: {} ({error})", path.display())
            }
        })?;
        #[cfg(unix)]
        file.try_lock()
            .map_err(|e| anyhow::anyhow!("Resource is in use: {} ({e})", path.display()))?;
        Ok(Self { file })
    }
}

pub(crate) struct MetadataLease {
    _lease: Lease,
}

impl MetadataLease {
    pub(crate) fn acquire(path: &Path) -> Result<Self> {
        Lease::acquire(path).map(|lease| Self { _lease: lease })
    }
}

impl Drop for MetadataLease {
    fn drop(&mut self) {
        // A concurrent fork can retain the open-file description until exec,
        // even with CLOEXEC set. Metadata ownership ends with this scope.
        // Storage leases instead survive in the guest until its final close.
        #[cfg(unix)]
        let _ = self._lease.file.unlock();
    }
}

pub fn atomic_write(path: &Path, data: &[u8]) -> Result<()> {
    let parent = path.parent().context("Missing parent directory")?;
    let mut file = tempfile::NamedTempFile::new_in(parent)?;
    file.write_all(data)?;
    file.as_file().sync_all()?;
    file.persist(path).map_err(|e| e.error)?;
    #[cfg(unix)]
    File::open(parent)?.sync_all()?;
    Ok(())
}

pub fn read_json(path: &Path) -> Result<Value> {
    serde_json::from_slice(&fs::read(path).with_context(|| path.display().to_string())?)
        .with_context(|| format!("Invalid JSON: {}", path.display()))
}

pub fn write_json(path: &Path, value: &impl Serialize) -> Result<()> {
    let mut bytes = serde_json::to_vec_pretty(value)?;
    bytes.push(b'\n');
    atomic_write(path, &bytes)
}

pub fn sha256(path: &Path) -> Result<String> {
    let mut file = File::open(path)?;
    let mut digest = Sha256::new();
    let mut bytes = [0u8; 65536];
    loop {
        let count = file.read(&mut bytes)?;
        if count == 0 {
            break;
        }
        digest.update(&bytes[..count]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
pub fn unhex(value: &str) -> Result<Vec<u8>> {
    ensure!(
        value.len().is_multiple_of(2) && value.is_ascii(),
        "Invalid hexadecimal bytes"
    );
    (0..value.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&value[i..i + 2], 16).map_err(Into::into))
        .collect()
}

fn member(name: &str) -> Result<String> {
    ensure!(
        !name.is_empty() && !name.starts_with('/') && !name.contains(['\\', '\0', ':']),
        "Invalid LPK member name"
    );
    let mut parts = Vec::new();
    for part in name.split('/') {
        ensure!(part != "..", "LPK archive paths must be relative");
        if !part.is_empty() && part != "." {
            parts.push(part);
        }
    }
    ensure!(!parts.is_empty(), "Empty LPK member name");
    Ok(parts.join("/"))
}

pub fn lpk_flash(path: &Path, layout: &Layout, mut flash: Vec<u8>) -> Result<Vec<u8>> {
    ensure!(flash.len() == layout.flash_bytes, "Flash size mismatch");
    let mut zip = zip::ZipArchive::new(File::open(path)?)?;
    ensure!(zip.len() <= 256, "LPK has too many archive entries");
    let mut names = HashMap::new();
    for i in 0..zip.len() {
        let entry = zip.by_index(i)?;
        let name = member(entry.name())?;
        ensure!(
            names.insert(name, i).is_none(),
            "Duplicate LPK archive member"
        );
    }
    let mut entry = zip.by_index(*names.get("manifest.json").context("LPK manifest missing")?)?;
    ensure!(entry.size() <= 256 * 1024, "LPK manifest too large");
    let mut bytes = Vec::new();
    entry
        .by_ref()
        .take(256 * 1024 + 1)
        .read_to_end(&mut bytes)?;
    ensure!(bytes.len() <= 256 * 1024, "LPK manifest too large");
    let manifest: Value = serde_json::from_slice(&bytes)?;
    ensure!(
        manifest["manifest"].as_u64() == Some(2),
        "Only LPK manifest version 2 is supported"
    );
    ensure!(
        manifest["chip"].as_str() == Some(&layout.chip),
        "LPK chip does not match this instance"
    );
    let images = manifest["images"]
        .as_array()
        .context("LPK images missing")?;
    ensure!(
        (1..=128).contains(&images.len()),
        "LPK must contain 1..128 images"
    );
    drop(entry);
    let mut ranges = Vec::new();
    for image in images {
        let name = member(
            image["file"]
                .as_str()
                .context("LPK image filename missing")?,
        )?;
        let mut entry = zip.by_index(*names.get(&name).context("LPK image missing")?)?;
        ensure!(
            !entry.is_dir()
                && entry
                    .unix_mode()
                    .map(|m| m & 0o170000 == 0 || m & 0o170000 == 0o100000)
                    .unwrap_or(true),
            "LPK image is not a regular file"
        );
        let size = usize::try_from(entry.size())?;
        ensure!(
            size > 0 && size <= layout.flash_bytes,
            "LPK image exceeds Flash bounds"
        );
        let offset = if let Some(s) = image["addr"].as_str() {
            if let Some(s) = s.strip_prefix("0x").or_else(|| s.strip_prefix("0X")) {
                usize::from_str_radix(s, 16)?
            } else {
                s.parse()?
            }
        } else {
            usize::try_from(image["addr"].as_u64().context("Invalid LPK address")?)?
        };
        ensure!(
            offset <= layout.flash_bytes - size,
            "LPK image exceeds Flash bounds"
        );
        ensure!(
            ranges
                .iter()
                .all(|&(start, end)| offset >= end || offset + size <= start),
            "LPK images overlap"
        );
        let digest = image["md5"].as_str().context("LPK image MD5 missing")?;
        ensure!(
            digest.len() == 32 && digest.bytes().all(|b| b.is_ascii_hexdigit()),
            "Invalid LPK MD5"
        );
        let mut data = Vec::with_capacity(size);
        entry
            .by_ref()
            .take(size as u64 + 1)
            .read_to_end(&mut data)?;
        ensure!(
            data.len() == size && format!("{:x}", Md5::digest(&data)).eq_ignore_ascii_case(digest),
            "LPK image MD5 mismatch: {name}"
        );
        flash[offset..offset + size].copy_from_slice(&data);
        ranges.push((offset, offset + size));
    }
    Ok(flash)
}

fn manifest(layout: &Layout) -> Value {
    json!({"version":1,"chip":layout.chip,"board":layout.board,
           "flash_bytes":layout.flash_bytes,"otp_bytes":layout.otp_bytes})
}

pub fn validate(path: &Path, layout: &Layout) -> Result<()> {
    ensure!(
        read_json(&path.join("instance.json"))? == manifest(layout),
        "Unsupported instance manifest"
    );
    ensure!(
        fs::metadata(path.join("flash.bin"))?.len() == layout.flash_bytes as u64,
        "Instance Flash size mismatch"
    );
    ensure!(
        fs::metadata(path.join("otp.bin"))?.len() == layout.otp_bytes as u64,
        "Instance OTP size mismatch"
    );
    ensure!(
        layout.uid_bytes > 0
            && layout.uid_bytes <= 16
            && layout.uid_offset + layout.uid_bytes <= layout.otp_bytes,
        "Invalid chip UID layout"
    );
    Ok(())
}

pub fn locked(path: &Path, layout: &Layout) -> Result<Lease> {
    ensure!(
        path.join("instance.json").is_file(),
        "Instance manifest missing"
    );
    let lease = Lease::acquire(&path.join("instance.lock"))?;
    validate(path, layout)?;
    Ok(lease)
}

pub fn uid(path: &Path, layout: &Layout) -> Result<String> {
    let otp = fs::read(path.join("otp.bin"))?;
    ensure!(
        otp.len() == layout.otp_bytes && layout.uid_offset + layout.uid_bytes <= otp.len(),
        "Invalid OTP image"
    );
    Ok(hex(
        &otp[layout.uid_offset..layout.uid_offset + layout.uid_bytes]
    ))
}

/// UID policy for one creation request; permissions are never persisted.
#[derive(Default)]
pub struct UidOptions {
    pub uid: Option<String>,
    pub allow_unprefixed_uid: bool,
}

pub fn generate_uid(layout: &Layout) -> Result<String> {
    ensure!(
        (2..=16).contains(&layout.uid_bytes),
        "Unsupported chip UID length"
    );
    let mut bytes = vec![0; layout.uid_bytes];
    getrandom::fill(&mut bytes[2..])?;
    bytes[..2].copy_from_slice(&[0xff, 0x00]);
    Ok(hex(&bytes))
}

pub(crate) fn generate_unique_uid(
    layout: &Layout,
    used: &std::collections::BTreeSet<String>,
) -> Result<String> {
    unique_uid_with(used, || generate_uid(layout))
}

fn unique_uid_with(
    used: &std::collections::BTreeSet<String>,
    mut generate: impl FnMut() -> Result<String>,
) -> Result<String> {
    for _ in 0..128 {
        let value = generate()?;
        if !used.contains(&value) {
            return Ok(value);
        }
    }
    anyhow::bail!("Cannot generate a unique chip UID after 128 attempts")
}

pub fn parse_uid(layout: &Layout, value: &str, allow_unprefixed: bool) -> Result<Vec<u8>> {
    ensure!(
        (2..=16).contains(&layout.uid_bytes),
        "Unsupported chip UID length"
    );
    ensure!(
        value.len() == layout.uid_bytes * 2 && value.bytes().all(|b| b.is_ascii_hexdigit()),
        "Chip UID must contain exactly {} hexadecimal characters",
        layout.uid_bytes * 2
    );
    let bytes: Vec<_> = (0..value.len())
        .step_by(2)
        .map(|at| u8::from_str_radix(&value[at..at + 2], 16).unwrap())
        .collect();
    ensure!(
        bytes.starts_with(&[0xff, 0x00]) || allow_unprefixed,
        "Chip UID must use the default prefix (ff00); explicit allow_unprefixed_uid is required otherwise"
    );
    Ok(bytes)
}

pub fn create(path: &Path, layout: &Layout, package: Option<&Path>) -> Result<PathBuf> {
    create_with_uid(path, layout, package, &UidOptions::default())
}

pub fn create_with_uid(
    path: &Path, layout: &Layout, package: Option<&Path>, options: &UidOptions,
) -> Result<PathBuf> {
    ensure!(
        !options.allow_unprefixed_uid || options.uid.is_some(),
        "UID permission requires a specified UID"
    );
    let value = match &options.uid {
        Some(value) => value.clone(),
        None => generate_uid(layout)?,
    };
    let bytes = parse_uid(layout, &value, options.allow_unprefixed_uid)?;
    ensure!(!path.exists(), "Instance already exists");
    let parent = path.parent().context("Instance needs a parent directory")?;
    fs::create_dir_all(parent)?;
    let temporary = tempfile::Builder::new()
        .prefix(".lisa-instance-")
        .tempdir_in(parent)?;
    let mut flash = vec![255; layout.flash_bytes];
    if let Some(package) = package {
        flash = lpk_flash(package, layout, flash)?;
    }
    let mut otp = vec![0; layout.otp_bytes];
    ensure!(
        layout.uid_bytes <= 16 && layout.uid_offset + layout.uid_bytes <= otp.len(),
        "Invalid chip UID layout"
    );
    otp[layout.uid_offset..layout.uid_offset + layout.uid_bytes].copy_from_slice(&bytes);
    atomic_write(&temporary.path().join("flash.bin"), &flash)?;
    atomic_write(&temporary.path().join("otp.bin"), &otp)?;
    write_json(&temporary.path().join("instance.json"), &manifest(layout))?;
    File::create(temporary.path().join("instance.lock"))?;
    ensure!(!path.exists(), "Instance already exists");
    fs::rename(temporary.path(), path)?;
    Ok(path.canonicalize()?)
}

pub fn import(path: &Path, layout: &Layout, package: &Path) -> Result<()> {
    let _lease = locked(path, layout)?;
    let flash = lpk_flash(package, layout, fs::read(path.join("flash.bin"))?)?;
    atomic_write(&path.join("flash.bin"), &flash)
}

pub fn erase(path: &Path, layout: &Layout) -> Result<()> {
    let _lease = locked(path, layout)?;
    atomic_write(&path.join("flash.bin"), &vec![255; layout.flash_bytes])
}

pub fn write_flash(path: &Path, layout: &Layout, image: &Path, offset: usize) -> Result<()> {
    let _lease = locked(path, layout)?;
    let size = usize::try_from(fs::metadata(image)?.len())?;
    ensure!(
        size > 0 && offset <= layout.flash_bytes && size <= layout.flash_bytes - offset,
        "Image exceeds Flash bounds"
    );
    let data = fs::read(image)?;
    ensure!(data.len() == size, "Image changed while reading");
    let mut flash = fs::read(path.join("flash.bin"))?;
    flash[offset..offset + size].copy_from_slice(&data);
    atomic_write(&path.join("flash.bin"), &flash)
}

pub fn regenerate_uid(path: &Path, layout: &Layout) -> Result<String> {
    regenerate_uid_excluding(path, layout, &std::collections::BTreeSet::new(), None)
}

pub(crate) fn regenerate_uid_excluding(
    path: &Path,
    layout: &Layout,
    used: &std::collections::BTreeSet<String>,
    expected: Option<&str>,
) -> Result<String> {
    let _lease = locked(path, layout)?;
    let mut otp = fs::read(path.join("otp.bin"))?;
    let previous = uid(path, layout)?;
    ensure!(
        expected.is_none_or(|value| value == previous),
        "Chip UID changed; inspect the instance again"
    );
    let mut used = used.clone();
    used.insert(previous);
    let value = generate_unique_uid(layout, &used)?;
    let bytes = parse_uid(layout, &value, false)?;
    otp[layout.uid_offset..layout.uid_offset + layout.uid_bytes].copy_from_slice(&bytes);
    atomic_write(&path.join("otp.bin"), &otp)?;
    uid(path, layout)
}

#[cfg(all(test, unix))]
mod unix_tests {
    use super::*;
    use std::{
        os::{
            fd::AsRawFd,
            unix::{net::UnixStream, process::CommandExt},
        },
        process::Command,
        thread,
        time::Duration,
    };

    fn isolated(name: &str) -> bool {
        if std::env::var("LISEM_FORK_TEST").as_deref() == Ok(name) {
            return false;
        }
        // Only this process opens the fixture's lease. Parallel tests in the
        // parent cannot fork while it is open and retain an unrelated copy.
        let status = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", name, "--nocapture", "--test-threads=1"])
            .env("LISEM_FORK_TEST", name)
            .status()
            .unwrap();
        assert!(status.success(), "Isolated fork fixture failed: {name}");
        true
    }

    fn retained_by_unrelated_fork(lease: impl Sized, path: &Path) -> bool {
        let (mut ready, child_ready) = UnixStream::pair().unwrap();
        let (child_resume, mut resume) = UnixStream::pair().unwrap();
        ready
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        child_resume
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let ready_fd = child_ready.as_raw_fd();
        let resume_fd = child_resume.as_raw_fd();
        let spawning = thread::spawn(move || {
            let mut command = Command::new("/usr/bin/true");
            unsafe {
                command.pre_exec(move || {
                    let mut byte = 1u8;
                    if libc::write(ready_fd, (&byte as *const u8).cast(), 1) != 1
                        || libc::read(resume_fd, (&mut byte as *mut u8).cast(), 1) != 1
                    {
                        return Err(std::io::Error::last_os_error());
                    }
                    Ok(())
                });
            }
            command.spawn().unwrap().wait().unwrap()
        });
        ready.read_exact(&mut [0]).unwrap();
        drop(lease);
        let retained = Lease::acquire(path).is_err();
        resume.write_all(&[1]).unwrap();
        assert!(spawning.join().unwrap().success());
        assert!(Lease::acquire(path).is_ok());
        retained
    }

    #[test]
    fn metadata_lease_releases_while_unrelated_child_waits_to_exec() {
        if isolated("storage::unix_tests::metadata_lease_releases_while_unrelated_child_waits_to_exec") {
            return;
        }
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("device.lock");
        let lease = MetadataLease::acquire(&path).unwrap();
        assert!(Lease::acquire(&path).is_err());
        assert!(!retained_by_unrelated_fork(lease, &path));
    }

    #[test]
    fn storage_lease_survives_its_original_owner_until_child_exit() {
        if isolated("storage::unix_tests::storage_lease_survives_its_original_owner_until_child_exit") {
            return;
        }
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("instance.lock");
        let lease = Lease::acquire(&path).unwrap();
        assert!(retained_by_unrelated_fork(lease, &path));
    }
}

#[cfg(all(test, windows))]
mod windows_tests {
    use super::*;
    use std::{
        process::{Command, Stdio},
        thread,
        time::Duration,
    };

    #[test]
    fn inherited_lease_survives_its_original_owner() {
        if std::env::var_os("LISEM_TEST_LEASE_CHILD").is_some() {
            thread::sleep(Duration::from_secs(30));
            return;
        }
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("instance.lock");
        let lease = Lease::acquire(&path).unwrap();
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "storage::windows_tests::inherited_lease_survives_its_original_owner",
            ])
            .env("LISEM_TEST_LEASE_CHILD", "1")
            .stdin(Stdio::from(lease.file.try_clone().unwrap()))
            .stdout(Stdio::null())
            .spawn()
            .unwrap();
        drop(lease);
        let retained = Lease::acquire(&path).is_err();
        child.kill().unwrap();
        child.wait().unwrap();
        assert!(
            retained,
            "Child lost the instance lease when its owner closed it"
        );
        assert!(Lease::acquire(&path).is_ok());
    }
}

#[cfg(test)]
mod uid_tests {
    use super::*;
    use std::collections::BTreeSet;

    #[test]
    fn generated_collisions_retry_and_exhaustion_is_bounded() {
        let used = BTreeSet::from(["ff00000000000000".to_owned()]);
        let mut calls = 0;
        let uid = unique_uid_with(&used, || {
            calls += 1;
            Ok(if calls < 3 {
                "ff00000000000000"
            } else {
                "ff00000000000001"
            }
            .into())
        })
        .unwrap();
        assert_eq!(calls, 3);
        assert_eq!(uid, "ff00000000000001");
        let mut calls = 0;
        assert!(
            unique_uid_with(&used, || {
                calls += 1;
                Ok("ff00000000000000".into())
            })
            .is_err()
        );
        assert_eq!(calls, 128);
    }

    #[test]
    fn chip_length_controls_validation_and_generation() {
        let layout = Layout {
            chip: "test".into(),
            board: "test".into(),
            flash_bytes: 1,
            otp_bytes: 32,
            uid_offset: 0,
            uid_bytes: 12,
        };
        assert!(parse_uid(&layout, "ff00a37c102d9e65", false).is_err());
        assert_eq!(
            parse_uid(&layout, "ff0000000000000000000001", false)
                .unwrap()
                .len(),
            12
        );
        let generated = generate_uid(&layout).unwrap();
        assert_eq!(generated.len(), 24);
        assert!(generated.starts_with("ff00"));
    }
}
