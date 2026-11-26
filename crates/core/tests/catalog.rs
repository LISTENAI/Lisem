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
    let mut layout = catalog.layout("arcs-mini2").unwrap();
    layout.board = "arcs-mini".into();
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
    assert_eq!(item["board"], "arcs-mini2");
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
fn mini_alias_creates_canonical_instances_without_duplicate_catalog_entries() {
    let temporary = tempfile::tempdir().unwrap();
    let catalog = Catalog::open(&root(), temporary.path()).unwrap();
    assert!(catalog.boards.contains_key("arcs-mini2"));
    assert!(!catalog.boards.contains_key("arcs-mini"));
    for id in ["arcs-mini", "arcs-mini2"] {
        let item = catalog.create(id, None, None).unwrap();
        assert_eq!(item["board"], "arcs-mini2");
        assert_eq!(item["hardware"]["board"]["name"], "Arcs-Mini2");
        let path = PathBuf::from(item["path"].as_str().unwrap());
        assert_eq!(
            storage::read_json(&path.join("instance.json")).unwrap()["board"],
            "arcs-mini2"
        );
        assert_eq!(
            catalog.metadata(&path).unwrap()["hardware"]["board"]["id"],
            "arcs-mini2"
        );
    }
    assert!(catalog.create("arcs-mini-unknown", None, None).is_err());
}

#[test]
fn mini_alias_reopens_saved_instances_without_rewriting_storage() {
    let temporary = tempfile::tempdir().unwrap();
    let catalog = Catalog::open(&root(), temporary.path()).unwrap();
    // Both raw board definitions and older compiled metadata remain readable.
    for compiled in [false, true] {
        let item = catalog
            .create("arcs-mini2", None, Some("My device"))
            .unwrap();
        let path = PathBuf::from(item["path"].as_str().unwrap());
        let mut metadata = catalog.metadata(&path).unwrap();
        if compiled {
            metadata["hardware"]["board"] = item["hardware"]["board"].clone();
            metadata["hardware"]["board"]["definition"]["id"] = json!("arcs-mini");
            metadata["hardware"]["board"]["definition"]["name"] = json!("Arcs-Mini");
        }
        metadata["hardware"]["board"]["id"] = json!("arcs-mini");
        metadata["hardware"]["board"]["name"] = json!("Arcs-Mini");
        storage::write_json(&path.join("device.json"), &metadata).unwrap();
        let mut manifest = storage::read_json(&path.join("instance.json")).unwrap();
        manifest["board"] = json!("arcs-mini");
        storage::write_json(&path.join("instance.json"), &manifest).unwrap();
        let files = ["flash.bin", "otp.bin", "instance.json", "device.json"];
        let before = files.map(|name| fs::read(path.join(name)).unwrap());
        let reopened = Catalog::open(&root(), temporary.path()).unwrap();
        let restored = reopened.attach(&path).unwrap();
        assert_eq!(restored["board"], "arcs-mini2");
        assert_eq!(restored["uid"], item["uid"]);
        assert_eq!(restored["id"], item["id"]);
        assert_eq!(restored["name"], "My device");
        assert_eq!(
            restored["hardware"]["board"]["runtime"]["machine"],
            "arcs-board"
        );
        let layout = reopened.layout("arcs-mini2").unwrap();
        drop(storage::locked(&path, &layout).unwrap());
        for (name, expected) in files.into_iter().zip(before) {
            assert_eq!(fs::read(path.join(name)).unwrap(), expected, "{name}");
        }
        // Alias resolution must not relax any other manifest field.
        for (field, value) in [
            ("board", json!("arcs-mini3")),
            ("chip", json!("venusa")),
            ("otp_bytes", json!(1024)),
        ] {
            let mut invalid = manifest.clone();
            invalid[field] = value;
            storage::write_json(&path.join("instance.json"), &invalid).unwrap();
            assert!(storage::validate(&path, &layout).is_err());
        }
        storage::write_json(&path.join("instance.json"), &manifest).unwrap();
    }
}

#[test]
fn mini_alias_supports_single_flash_manifests_and_rejects_custom_shadowing() {
    let temporary = tempfile::tempdir().unwrap();
    let mut catalog = Catalog::open(&root(), &temporary.path().join("library")).unwrap();
    let layout = catalog.layout("arcs-mini2").unwrap();
    let path = temporary.path().join("old-device");
    storage::create(&path, &layout, None).unwrap();
    storage::write_json(
        &path.join("instance.json"),
        &json!({
            "version":1, "chip":"arcs", "board":"arcs-mini",
            "flash_bytes":layout.flash_bytes, "otp_bytes":layout.otp_bytes,
        }),
    )
    .unwrap();
    let before = ["flash.bin", "otp.bin"].map(|name| fs::read(path.join(name)).unwrap());
    assert_eq!(catalog.attach(&path).unwrap()["board"], "arcs-mini2");
    for (name, expected) in ["flash.bin", "otp.bin"].into_iter().zip(before) {
        assert_eq!(fs::read(path.join(name)).unwrap(), expected);
    }
    let mut custom = storage::read_json(&root().join("boards/arcs-mini2.json")).unwrap();
    custom["id"] = json!("arcs-mini");
    let source = temporary.path().join("custom.json");
    storage::write_json(&source, &custom).unwrap();
    assert!(
        catalog
            .import_board(&source)
            .unwrap_err()
            .to_string()
            .contains("Installed board IDs")
    );
    fs::create_dir_all(catalog.data.join("boards")).unwrap();
    storage::write_json(&catalog.data.join("boards/custom.json"), &custom).unwrap();
    assert!(
        catalog
            .reload_hardware()
            .unwrap_err()
            .to_string()
            .contains("shadows")
    );
}

#[test]
fn presentation_does_not_change_hardware_identity() {
    let temporary = tempfile::tempdir().unwrap();
    let catalog = Catalog::open(&root(), temporary.path()).unwrap();
    let expected = json!({"board": catalog.boards["arcs-mini2"], "chip": catalog.chips["ls2684"]});
    let mut renamed = expected.clone();
    for pointer in [
        "/board/name",
        "/chip/name",
        "/board/screen/label",
        "/board/buttons/0/label",
        "/board/inputs/0/label",
        "/board/inputs/0/description",
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
        ("/board/flash_bytes", json!(4 * 1024 * 1024)),
    ] {
        let mut changed = renamed.clone();
        *changed.pointer_mut(pointer).unwrap() = value;
        assert!(catalog.check_hardware(&changed).is_err(), "{pointer}");
    }
}

#[test]
fn camera_descriptor_enriches_existing_mini_without_changing_storage() {
    let temporary = tempfile::tempdir().unwrap();
    let catalog = Catalog::open(&root(), temporary.path()).unwrap();
    let item = catalog.create("arcs-mini2", None, None).unwrap();
    let path = PathBuf::from(item["path"].as_str().unwrap());
    let flash_hash = storage::sha256(&path.join("flash.bin")).unwrap();
    let otp_hash = storage::sha256(&path.join("otp.bin")).unwrap();
    let mut metadata: serde_json::Value =
        serde_json::from_slice(&fs::read(path.join("device.json")).unwrap()).unwrap();
    metadata["hardware"]["board"]
        .as_object_mut()
        .unwrap()
        .remove("camera");
    storage::write_json(&path.join("device.json"), &metadata).unwrap();
    let described = catalog.describe(&path).unwrap();
    assert_eq!(described["hardware"]["board"]["camera"]["model"], "gc0328");
    assert_eq!(described["uid"], item["uid"]);
    assert_eq!(
        storage::sha256(&path.join("flash.bin")).unwrap(),
        flash_hash
    );
    assert_eq!(storage::sha256(&path.join("otp.bin")).unwrap(), otp_hash);
    let updated = catalog
        .update(&path, &json!({"camera_image":"input.png"}))
        .unwrap();
    assert_eq!(updated["host"]["camera_image"], "input.png");
    assert!(
        catalog
            .update(&path, &json!({"camera_image":null}))
            .unwrap()["host"]["camera_image"]
            .is_null()
    );
    let mut legacy_camera = described["hardware"].clone();
    legacy_camera["board"]["camera"]
        .as_object_mut()
        .unwrap()
        .remove("rotation_clockwise");
    metadata["hardware"] = legacy_camera;
    storage::write_json(&path.join("device.json"), &metadata).unwrap();
    let oriented = catalog.describe(&path).unwrap();
    assert_eq!(
        oriented["hardware"]["board"]["camera"]["rotation_clockwise"],
        90
    );
    assert_eq!(
        storage::sha256(&path.join("flash.bin")).unwrap(),
        flash_hash
    );
    assert_eq!(storage::sha256(&path.join("otp.bin")).unwrap(), otp_hash);
    // Saved presentation metadata is replaced by the installed board source.
    let mut saved = oriented["hardware"].clone();
    saved["board"]["camera"]["rotation_clockwise"] = json!(0);
    assert_eq!(catalog.check_hardware(&saved).unwrap()["board"]["camera"]["rotation_clockwise"], 90);

}

#[test]
fn evb_touch_mount_enriches_saved_descriptor() {
    let temporary = tempfile::tempdir().unwrap();
    let catalog = Catalog::open(&root(), temporary.path()).unwrap();
    let expected = json!({"board": catalog.boards["arcs-evb"], "chip": catalog.chips["ls2684"]});
    let mut saved = expected.clone();
    saved["board"]["touch"].as_object_mut().unwrap().remove("rotation_clockwise");
    assert_eq!(catalog.check_hardware(&saved).unwrap(), expected);
    saved["board"]["touch"]["rotation_clockwise"] = json!(90);
    assert_eq!(catalog.check_hardware(&saved).unwrap(), expected);
}

#[test]
fn board_inputs_enrich_existing_instances_and_validate_persisted_values() {
    let temporary = tempfile::tempdir().unwrap();
    let catalog = Catalog::open(&root(), temporary.path()).unwrap();
    let item = catalog.create("arcs-mini2", None, None).unwrap();
    let path = PathBuf::from(item["path"].as_str().unwrap());
    let before = [storage::sha256(&path.join("flash.bin")).unwrap(),
        storage::sha256(&path.join("otp.bin")).unwrap()];
    let mut metadata = storage::read_json(&path.join("device.json")).unwrap();
    metadata["hardware"]["board"].as_object_mut().unwrap().remove("inputs");
    storage::write_json(&path.join("device.json"), &metadata).unwrap();
    let enriched = catalog.describe(&path).unwrap();
    let input = |value: &serde_json::Value, id: &str| value["inputs"].as_array().unwrap()
        .iter().find(|v|v["id"]==id).unwrap()["value"].clone();
    assert_eq!(input(&enriched, "usb_det"), true);
    assert_eq!(input(&enriched, "battery_voltage"), 4200);
    let changed = catalog.update(&path, &json!({"inputs":{"usb_det":false}})).unwrap();
    assert_eq!(input(&changed, "usb_det"), false);
    assert!(catalog.update(&path, &json!({"inputs":{"usb_det":1}})).is_err());
    assert_eq!(input(&catalog.describe(&path).unwrap(), "usb_det"), false);
    assert_eq!(storage::sha256(&path.join("flash.bin")).unwrap(), before[0]);
    assert_eq!(storage::sha256(&path.join("otp.bin")).unwrap(), before[1]);

}

#[test]
fn runtime_metadata_updates_preserve_instances_and_current_availability() {
    let temporary = tempfile::tempdir().unwrap();
    let mut catalog = Catalog::open(&root(), temporary.path()).unwrap();
    let item = catalog.create("arcs-mini2", None, None).unwrap();
    let path = PathBuf::from(item["path"].as_str().unwrap());
    let before = ["flash.bin", "otp.bin", "device.json"].map(|file| {
        storage::sha256(&path.join(file)).unwrap()
    });
    catalog.boards.get_mut("arcs-mini2").unwrap()["runtime"] = json!({
        "machine":"arcs-mini", "available":false,
        "unavailable_reason":"Runtime validation pending",
        "capabilities":{"snapshot":false}
    });
    catalog.chips.get_mut("ls2684").unwrap()["runtime"] = json!({
        "abi":"lisem-v1", "environment_prefix":"ARCS_QEMU_",
        "clock_quantum_ns":20000, "flash":"bios"
    });
    let current = catalog.describe(&path).unwrap();
    assert_eq!(current["uid"], item["uid"]);
    assert_eq!(current["capabilities"]["snapshot"], false);
    assert_eq!(current["hardware"]["chip"]["runtime"]["clock_quantum_ns"], 20000);
    let error = lisem_core::assets::Backend::for_device(&catalog, &current).err().unwrap();
    assert_eq!(error.to_string(), "Runtime validation pending");
    catalog.boards.get_mut("arcs-mini2").unwrap()["runtime"]["available"] = json!(true);
    catalog.boards.get_mut("arcs-mini2").unwrap()["runtime"]["capabilities"]["snapshot"] = json!(true);
    let current = catalog.describe(&path).unwrap();
    assert!(lisem_core::assets::Backend::for_device(&catalog, &current).unwrap().capabilities.snapshot);
    for (file, hash) in ["flash.bin", "otp.bin", "device.json"].into_iter().zip(before) {
        assert_eq!(storage::sha256(&path.join(file)).unwrap(), hash);
    }
    for pointer in ["/board/flash_bytes", "/chip/storage/otp_bytes",
        "/chip/storage/uid_offset"] {
        let mut different = current["hardware"].clone();
        match pointer {
            "/chip/storage/otp_bytes" => different["chip"]["storage"] = json!({"otp_bytes":64}),
            "/chip/storage/uid_offset" => different["chip"]["storage"] = json!({"uid_offset":0}),
            _ => *different.pointer_mut(pointer).unwrap() = json!(123),
        }
        assert!(catalog.check_hardware(&different).is_err(), "{pointer}");
    }
}
