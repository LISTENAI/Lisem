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
        flash_devices: vec![],
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

fn dual_layout() -> Layout {
    use storage::FlashDevice;
    Layout { flash_bytes: 196608,
        flash_devices: vec![FlashDevice::new("boot",0,65536), FlashDevice::new("data",1,131072)],
        ..layout() }
}

#[test]
fn unequal_nor_images_preserve_boundaries_and_reject_geometry_changes() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("device");
    let layout = dual_layout();
    storage::create(&path, &layout, None).unwrap();
    let otp = fs::read(path.join("otp.bin")).unwrap();
    let image = temp.path().join("image.bin");
    fs::write(&image, [0x12,0x34,0x56,0x78]).unwrap();
    storage::write_flash(&path, &layout, &image, 65534).unwrap();
    let first = fs::read(path.join("flash.bin")).unwrap();
    let second = fs::read(path.join("flash-cs1.bin")).unwrap();
    assert_eq!(first.len(),65536); assert_eq!(second.len(),131072);
    assert_eq!(&first[65534..], &[0x12,0x34]);
    assert_eq!(&second[..2], &[0x56,0x78]);
    let mut wrong = layout.clone();
    wrong.flash_devices[0].capacity_bytes = 131072;
    wrong.flash_devices[1].capacity_bytes = 65536;
    assert!(storage::locked(&path,&wrong).is_err());
    let lpk = temp.path().join("test.lpk");
    package(&lpk,"test-family",65530,true);
    assert!(storage::import(&path,&layout,&lpk).is_err());
    assert_eq!(fs::read(path.join("flash.bin")).unwrap(),first);
    assert_eq!(fs::read(path.join("flash-cs1.bin")).unwrap(),second);
    package(&lpk,"test-family",65530,false);
    storage::import(&path,&layout,&lpk).unwrap();
    assert_eq!(&storage::read_flash_images(&path,&layout).unwrap()[65530..65547],b"original firmware");
    assert!(storage::write_flash(&path,&layout,&image,layout.flash_bytes-1).is_err());
    storage::erase(&path,&layout).unwrap();
    assert!(storage::read_flash_images(&path,&layout).unwrap().iter().all(|b|*b==255));
    assert_eq!(fs::read(path.join("otp.bin")).unwrap(),otp);
}

#[test]
fn single_flash_manifest_upgrade_preserves_images_and_requires_lock() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("device");
    let layout = layout();
    storage::create(&path, &layout, None).unwrap();
    let otp = fs::read(path.join("otp.bin")).unwrap();
    let bytes: Vec<u8> = (0..layout.flash_bytes).map(|i| (i % 251) as u8).collect();
    fs::write(path.join("flash.bin"), &bytes).unwrap();
    let saved = json!({"version":1,"chip":layout.chip,
        "board":layout.board,"flash_bytes":layout.flash_bytes,"otp_bytes":layout.otp_bytes});
    storage::write_json(&path.join("instance.json"), &saved).unwrap();
    let lease = storage::Lease::acquire(&path.join("instance.lock")).unwrap();
    assert!(storage::locked(&path, &layout).is_err());
    assert_eq!(storage::read_json(&path.join("instance.json")).unwrap(), saved);
    drop(lease);
    for _ in 0..2 {
        drop(storage::locked(&path, &layout).unwrap());
        assert_eq!(fs::read(path.join("flash.bin")).unwrap(), bytes);
        assert_eq!(fs::read(path.join("otp.bin")).unwrap(), otp);
        assert_eq!(storage::read_json(&path.join("instance.json")).unwrap()["version"], 2);
    }
}

#[test]
fn multi_flash_requires_per_device_manifest() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("device");
    let layout = dual_layout();
    storage::create(&path, &layout, None).unwrap();
    let before = storage::read_flash_images(&path, &layout).unwrap();
    let saved = json!({"version":1,"chip":layout.chip,
        "board":layout.board,"flash_bytes":layout.flash_bytes,"otp_bytes":layout.otp_bytes});
    storage::write_json(&path.join("instance.json"), &saved).unwrap();
    assert!(storage::locked(&path, &layout).is_err());
    assert_eq!(storage::read_json(&path.join("instance.json")).unwrap(), saved);
    assert_eq!(storage::read_flash_images(&path, &layout).unwrap(), before);
}
