use lisem_core::{catalog::Catalog, storage};
use serde_json::json;
use std::{fs, path::PathBuf};

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

#[test]
fn legacy_index_migration_preserves_storage_and_instance_identity() {
    let temporary = tempfile::tempdir().unwrap();
    let data = temporary.path().join("library");
    let catalog = Catalog::open(&root(), &data).unwrap();
    let layout = catalog.layout("arcs-mini").unwrap();
    let path = temporary.path().join("device");
    storage::create(&path, &layout, None).unwrap();
    let before: Vec<_> = ["flash.bin", "otp.bin", "instance.json"]
        .iter()
        .map(|name| fs::read(path.join(name)).unwrap())
        .collect();
    let id = "a24c7a1f77004c4aad556823ba156f8d";
    storage::write_json(
        &data.join("devices.json"),
        &json!({"version":1,"devices":[
            {"id":id,"name":"Existing device","path":path,"firmware":"original.lpk"}
        ]}),
    )
    .unwrap();
    let reopened = Catalog::open(&root(), &data).unwrap();
    let item = reopened.device(id).unwrap();
    assert_eq!(item["id"], id);
    assert_eq!(item["name"], "Existing device");
    assert!(item.get("firmware").is_none());
    for (name, expected) in ["flash.bin", "otp.bin", "instance.json"].iter().zip(before) {
        assert_eq!(fs::read(path.join(name)).unwrap(), expected);
    }
    let moved = temporary.path().join("moved");
    fs::rename(&path, &moved).unwrap();
    assert_eq!(reopened.attach(&moved).unwrap()["id"], id);
    assert_eq!(
        reopened.device(id).unwrap()["path"],
        json!(moved.canonicalize().unwrap())
    );
}

#[test]
fn presentation_does_not_change_hardware_identity() {
    let temporary = tempfile::tempdir().unwrap();
    let catalog = Catalog::open(&root(), temporary.path()).unwrap();
    let expected = json!({"board": catalog.boards["arcs-mini"], "chip": catalog.chips["ls2684"]});
    let mut renamed = expected.clone();
    for pointer in [
        "/board/name",
        "/chip/name",
        "/board/screen/label",
        "/board/buttons/0/label",
        "/board/indicators/0/label",
    ] {
        *renamed.pointer_mut(pointer).unwrap() = json!("Alternative display name");
    }
    let before = renamed.clone();
    assert_eq!(catalog.check_hardware(&renamed).unwrap(), expected);
    assert_eq!(renamed, before);
    for indicators in [
        json!([]),
        json!([{"id":"custom","kind":"signal","bank":"A","pin":4,
            "active_low":false,"color":"#ffffff","label":"Custom signal"}]),
    ] {
        let mut changed = renamed.clone();
        changed["board"]["indicators"] = indicators;
        assert_eq!(catalog.check_hardware(&changed).unwrap(), expected);
    }
    let mut unobserved = renamed.clone();
    unobserved["board"]
        .as_object_mut()
        .unwrap()
        .remove("indicators");
    assert_eq!(catalog.check_hardware(&unobserved).unwrap(), expected);
    for (pointer, value) in [
        ("/board/id", json!("different-board")),
        ("/chip/id", json!("different-chip")),
        ("/board/chip", json!("different-chip")),
        ("/board/version", json!(2)),
        ("/board/flash_bytes", json!(4 * 1024 * 1024)),
        ("/board/buttons/0/id", json!("different-button")),
        ("/board/buttons/0/pin", json!(5)),
        ("/board/buttons/0/active_low", json!(false)),
        ("/board/screen/width", json!(320)),
    ] {
        let mut changed = renamed.clone();
        *changed.pointer_mut(pointer).unwrap() = value;
        assert!(catalog.check_hardware(&changed).is_err(), "{pointer}");
    }
}
