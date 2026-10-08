use lisem_core::{catalog::Catalog, manager::Manager, storage};
use serde_json::json;
use std::{
    fs,
    path::{Path, PathBuf},
};

#[test]
fn management_preserves_storage_and_rejects_busy_mutations() {
    let temp = tempfile::tempdir().unwrap();
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let mut manager = Manager::new(
        &root,
        &temp.path().join("library"),
        Path::new("unused-worker"),
    )
    .unwrap();
    let item = manager
        .call("create", json!({"board":"arcs-mini"}))
        .unwrap();
    let path = Path::new(item["path"].as_str().unwrap());
    let before = ["flash.bin", "otp.bin"].map(|name| fs::read(path.join(name)).unwrap());
    let layout = manager.catalog.layout("arcs-mini").unwrap();
    let lease = storage::locked(path, &layout).unwrap();
    assert!(
        manager
            .call("erase", json!({"id":item["id"],"confirm_uid":item["uid"]}))
            .is_err()
    );
    assert!(
        manager
            .call("settings", json!({"id":item["id"],"online":false}))
            .is_err()
    );
    manager
        .call("rename", json!({"id":item["id"],"name":"Example device"}))
        .unwrap();
    drop(lease);
    manager
        .call(
            "settings",
            json!({"id":item["id"],"online":false,"microphone":true,"uart":[0]}),
        )
        .unwrap();
    assert!(
        manager
            .call(
                "regenerate_uid",
                json!({"id":item["id"],"confirm_uid":"wrong"})
            )
            .is_err()
    );
    manager.call("detach", json!({"id":item["id"]})).unwrap();
    let second = Catalog::open(&root, &temp.path().join("second-library")).unwrap();
    let restored = second.attach(path).unwrap();
    assert_eq!(restored["id"], item["id"]);
    assert_eq!(restored["uid"], item["uid"]);
    assert_eq!(restored["name"], "Example device");
    assert_eq!(restored["host"]["online"], false);
    assert_eq!(restored["host"]["microphone"], true);
    assert_eq!(restored["host"]["uart"], json!([0]));
    for (name, bytes) in ["flash.bin", "otp.bin"].iter().zip(before) {
        assert_eq!(fs::read(path.join(name)).unwrap(), bytes);
    }
}
