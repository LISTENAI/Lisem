use lisem_core::{
    catalog::Catalog,
    manager::Manager,
    storage::{self, UidOptions},
};
use serde_json::json;
use std::{
    fs,
    path::{Path, PathBuf},
};

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}
fn options(uid: &str, allow: bool) -> UidOptions {
    UidOptions {
        uid: Some(uid.into()),
        allow_unprefixed_uid: allow,
    }
}

#[test]
fn uid_policy_is_shared_and_request_scoped() {
    let temp = tempfile::tempdir().unwrap();
    let mut manager = Manager::new(&root(), temp.path(), Path::new("unused-worker")).unwrap();
    for request in [
        json!({"uid":123}),
        json!({"uid":true}),
        json!({"uid":""}),
        json!({"uid":"ff00a37c102d9e6"}),
        json!({"uid":"ff00a37c102d9e6500"}),
        json!({"uid":"ff00a37c102d9e6g"}),
        json!({"uid":"ｆｆ００a37c102d"}),
        json!({"uid":" ff00a37c102d9e65"}),
        json!({"uid":"0xff00a37c102d9e65"}),
        json!({"uid":"0102a37c102d9e65"}),
        json!({"allow_unprefixed_uid":true}),
        json!({"allow_unprefixed_uid":"true"}),
        json!({"uid":"bad", "allow_unprefixed_uid":true}),
    ] {
        let mut request = request;
        request["board"] = json!("arcs-mini");
        assert!(
            manager.call("create", request.clone()).is_err(),
            "{request}"
        );
        assert!(manager.catalog.entries().unwrap().is_empty());
    }
    let custom = manager
        .call(
            "create",
            json!({"board":"arcs-mini","uid":"FF00A37C102D9E65"}),
        )
        .unwrap();
    assert_eq!(custom["uid"], "ff00a37c102d9e65");
    assert_ne!(custom["id"], custom["uid"]);
    for board in ["arcs-mini", "arcs-mini3", "venusa-rd-evb"] {
        assert!(
            manager
                .call(
                    "create",
                    json!({"board":board, "uid":"FF00A37C102D9E65", "allow_unprefixed_uid":true})
                )
                .is_err()
        );
    }
    let allowed = manager
        .call(
            "create",
            json!({"board":"arcs-mini3","uid":"0102a37c102d9e65", "allow_unprefixed_uid":true}),
        )
        .unwrap();
    assert_eq!(allowed["uid"], "0102a37c102d9e65");
    assert!(
        manager
            .call(
                "create",
                json!({"board":"arcs-mini","uid":"0102a37c102d9e66"})
            )
            .is_err()
    );
    let automatic = manager
        .call("create", json!({"board":"arcs-mini"}))
        .unwrap();
    assert!(automatic["uid"].as_str().unwrap().starts_with("ff00"));
    let path = Path::new(custom["path"].as_str().unwrap());
    let layout = manager.catalog.layout("arcs-mini").unwrap();
    let lease = storage::locked(path, &layout).unwrap();
    assert!(
        manager
            .call(
                "regenerate_uid",
                json!({"id":custom["id"],"confirm_uid":custom["uid"]})
            )
            .is_err()
    );
    drop(lease);
    assert!(
        manager
            .catalog
            .regenerate_uid(path, &layout, "wrong")
            .is_err()
    );
    let new = manager
        .call(
            "regenerate_uid",
            json!({"id":custom["id"],"confirm_uid":custom["uid"]}),
        )
        .unwrap();
    assert!(new["uid"].as_str().unwrap().starts_with("ff00"));
    assert_ne!(new["uid"], custom["uid"]);
    assert_ne!(new["uid"], automatic["uid"]);
    assert!(
        manager
            .call(
                "regenerate_uid",
                json!({"id":custom["id"],"confirm_uid":custom["uid"]})
            )
            .is_err()
    );
}

#[test]
fn current_chip_layouts_preserve_non_uid_storage() {
    let temp = tempfile::tempdir().unwrap();
    let catalog = Catalog::open(&root(), temp.path()).unwrap();
    for (board, offset, otp_bytes) in [
        ("arcs-mini", 8, 512),
        ("arcs-mini3", 8, 512),
        ("venusa-rd-evb", 0, 64),
    ] {
        let item = if board.starts_with("arcs-") {
            catalog.create_with_uid(board, None, None, &options("FF00A37C102D9E65", false))
        } else {
            catalog.create(board, None, None)
        }
        .unwrap();
        let path = Path::new(item["path"].as_str().unwrap());
        let layout = catalog.layout(board).unwrap();
        assert_eq!(
            (layout.uid_offset, layout.otp_bytes, layout.uid_bytes),
            (offset, otp_bytes, 8)
        );
        let mut otp = fs::read(path.join("otp.bin")).unwrap();
        if board.starts_with("arcs-") {
            let bytes = &otp[offset..offset + 8];
            assert_eq!(bytes, [0xff, 0, 0xa3, 0x7c, 0x10, 0x2d, 0x9e, 0x65]);
            assert_eq!(
                u32::from_le_bytes(bytes[..4].try_into().unwrap()),
                0x7ca300ff
            );
            assert_eq!(
                u32::from_le_bytes(bytes[4..].try_into().unwrap()),
                0x659e2d10
            );
            assert_eq!(
                u64::from_le_bytes(bytes.try_into().unwrap()),
                0x659e2d107ca300ff
            );
        }
        for (i, byte) in otp.iter_mut().enumerate() {
            if !(offset..offset + 8).contains(&i) {
                *byte = (i % 251) as u8;
            }
        }
        fs::write(path.join("otp.bin"), &otp).unwrap();
        let flash = fs::read(path.join("flash.bin")).unwrap();
        let uid = catalog
            .regenerate_uid(path, &layout, item["uid"].as_str().unwrap())
            .unwrap();
        assert!(uid[..4].eq_ignore_ascii_case("ff00"));
        let new = fs::read(path.join("otp.bin")).unwrap();
        assert_eq!(&new[..offset], &otp[..offset]);
        assert_eq!(&new[offset + 8..], &otp[offset + 8..]);
        assert_eq!(fs::read(path.join("flash.bin")).unwrap(), flash);
        let reopened = Catalog::open(&root(), temp.path()).unwrap();
        assert_eq!(
            reopened.device(item["id"].as_str().unwrap()).unwrap()["uid"],
            uid
        );
    }
}

#[test]
fn legacy_identity_is_unchanged_and_failed_creation_leaves_no_instance() {
    let temp = tempfile::tempdir().unwrap();
    let catalog = Catalog::open(&root(), temp.path()).unwrap();
    let item = catalog
        .create_with_uid("arcs-mini", None, None, &options("0102a37c102d9e65", true))
        .unwrap();
    let path = Path::new(item["path"].as_str().unwrap());
    let before = ["flash.bin", "otp.bin", "instance.json", "device.json"]
        .map(|name| fs::read(path.join(name)).unwrap());
    let reopened = Catalog::open(&root(), temp.path()).unwrap();
    assert_eq!(
        reopened.device(item["id"].as_str().unwrap()).unwrap()["uid"],
        item["uid"]
    );
    for (name, expected) in ["flash.bin", "otp.bin", "instance.json", "device.json"]
        .iter()
        .zip(before)
    {
        assert_eq!(fs::read(path.join(name)).unwrap(), expected);
    }
    let entries = catalog.entries().unwrap();
    assert!(
        catalog
            .create_with_uid(
                "arcs-mini",
                Some(&temp.path().join("missing.lpk")),
                None,
                &options("ff00a37c102d9e65", false)
            )
            .is_err()
    );
    assert_eq!(catalog.entries().unwrap(), entries);
    assert_eq!(
        fs::read_dir(temp.path().join("devices")).unwrap().count(),
        1
    );
    // A failed request does not reserve the requested identity.
    assert!(
        catalog
            .create_with_uid("arcs-mini", None, None, &options("ff00a37c102d9e65", false))
            .is_ok()
    );
}
