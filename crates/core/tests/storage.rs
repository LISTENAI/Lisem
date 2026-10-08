use lisem_core::storage::{self, Layout};
use md5::{Digest, Md5};
use serde_json::json;
use std::{
    fs::{self, File},
    io::Write,
    path::Path,
};

fn layout() -> Layout {
    Layout {
        chip: "test-family".into(),
        board: "test-board".into(),
        flash_bytes: 8192,
        otp_bytes: 512,
        uid_offset: 8,
        uid_bytes: 8,
    }
}

fn package(path: &Path, chip: &str, offset: usize, wrong_md5: bool) {
    let bytes = b"original firmware";
    let md5 = if wrong_md5 {
        "0".repeat(32)
    } else {
        format!("{:x}", Md5::digest(bytes))
    };
    let mut zip = zip::ZipWriter::new(File::create(path).unwrap());
    let options = zip::write::SimpleFileOptions::default();
    zip.start_file("manifest.json", options).unwrap();
    zip.write_all(
        serde_json::to_string(&json!({"manifest":2,"chip":chip,
        "images":[{"file":"firmware.bin","addr":offset,"md5":md5}]}))
        .unwrap()
        .as_bytes(),
    )
    .unwrap();
    zip.start_file("firmware.bin", options).unwrap();
    zip.write_all(bytes).unwrap();
    zip.finish().unwrap();
}

#[test]
fn instance_operations_preserve_independent_identity_and_unlisted_flash() {
    let temp = tempfile::tempdir().unwrap();
    let a = temp.path().join("a");
    let b = temp.path().join("b");
    let layout = layout();
    storage::create(&a, &layout, None).unwrap();
    storage::create(&b, &layout, None).unwrap();
    assert_ne!(
        storage::uid(&a, &layout).unwrap(),
        storage::uid(&b, &layout).unwrap()
    );
    let otp = fs::read(a.join("otp.bin")).unwrap();
    let image = temp.path().join("data.bin");
    fs::write(&image, [1, 2, 3]).unwrap();
    storage::write_flash(&a, &layout, &image, 4096).unwrap();
    let lpk = temp.path().join("test.lpk");
    package(&lpk, "test-family", 16, false);
    storage::import(&a, &layout, &lpk).unwrap();
    let flash = fs::read(a.join("flash.bin")).unwrap();
    assert_eq!(&flash[16..33], b"original firmware");
    assert_eq!(&flash[4096..4099], &[1, 2, 3]);
    assert_eq!(fs::read(a.join("otp.bin")).unwrap(), otp);
    let lease = storage::locked(&a, &layout).unwrap();
    assert!(storage::erase(&a, &layout).is_err());
    assert!(storage::regenerate_uid(&a, &layout).is_err());
    drop(lease);
    storage::erase(&a, &layout).unwrap();
    assert!(
        fs::read(a.join("flash.bin"))
            .unwrap()
            .iter()
            .all(|&b| b == 255)
    );
    assert_eq!(fs::read(a.join("otp.bin")).unwrap(), otp);
    storage::regenerate_uid(&a, &layout).unwrap();
    let new = fs::read(a.join("otp.bin")).unwrap();
    assert_ne!(&new[8..16], &otp[8..16]);
    assert_eq!(&new[..8], &otp[..8]);
    assert_eq!(&new[16..], &otp[16..]);
}

#[test]
fn malformed_or_incompatible_packages_do_not_modify_storage() {
    let temp = tempfile::tempdir().unwrap();
    let instance = temp.path().join("device");
    let layout = layout();
    storage::create(&instance, &layout, None).unwrap();
    let before = fs::read(instance.join("flash.bin")).unwrap();
    for (chip, offset, bad) in [
        ("another-chip", 16, false),
        ("test-family", 8190, false),
        ("test-family", 16, true),
    ] {
        let path = temp.path().join("bad.lpk");
        package(&path, chip, offset, bad);
        assert!(storage::import(&instance, &layout, &path).is_err());
        assert_eq!(fs::read(instance.join("flash.bin")).unwrap(), before);
    }
    assert!(storage::create(&instance, &layout, None).is_err());
}
