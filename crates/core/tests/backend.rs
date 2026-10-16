use lisem_core::{assets::Backend, catalog::Catalog};
use serde_json::{Value, json};
use std::{path::PathBuf, process::Command};

fn minimal() -> Value {
    json!({"chip":{"id":"test-chip","runtime":{"abi":"lisem-v1",
        "environment_prefix":"LISEM_QEMU_","flash":"pflash"}},
        "board":{"id":"test-board","runtime":{"machine":"test-board"},
        "uart_channels":[0,1],"buttons":[],"audio":false}})
}

#[test]
fn serial_only_machine_has_no_implicit_media_or_usb_contract() {
    let backend = Backend::from_hardware(&minimal()).unwrap();
    let caps = &backend.capabilities;
    assert!(!caps.snapshot && !caps.screen && !caps.audio && !caps.camera);
    assert!(!caps.wifi && !caps.sd && !caps.usb_adb && !caps.signals);
    assert_eq!(caps.uart_channels, [0, 1]);
    let mut command = Command::new("qemu");
    backend
        .configure(
            &mut command,
            "device".as_ref(),
            "output".as_ref(),
            10,
            false,
            false,
        )
        .unwrap();
    let args: Vec<_> = command
        .get_args()
        .map(|a| a.to_string_lossy().to_string())
        .collect();
    assert!(
        args.iter()
            .any(|a| a.starts_with("if=pflash,format=raw,file="))
    );
    assert!(!args.iter().any(|a| a == "-bios" || a.contains("sdcard")));
    assert!(
        !command
            .get_envs()
            .any(|(k, _)| k.to_string_lossy().starts_with("ARCS_"))
    );
    assert!(
        backend
            .configure(
                &mut command,
                "device".as_ref(),
                "output".as_ref(),
                10,
                true,
                false
            )
            .is_err()
    );
    assert!(backend.button("function").is_err());
}

#[test]
fn controls_follow_declared_buttons_and_sensor_geometry() {
    let mut hardware = minimal();
    hardware["board"]["buttons"] = json!([{"id":"key"}]);
    hardware["board"]["touch"] = json!({"width":640,"height":480});
    hardware["board"]["runtime"]["buttons"] = json!({"key":"key-pressed"});
    hardware["board"]["runtime"]["button_sequence_ids"] = json!(["key"]);
    hardware["board"]["runtime"]["touch_property"] = json!("touch-input");
    hardware["board"]["runtime"]["capabilities"] = json!({"button_sequence":true});
    let backend = Backend::from_hardware(&hardware).unwrap();
    assert_eq!(backend.button("key").unwrap(), "key-pressed");
    assert_eq!(backend.sequence_id("key").unwrap(), Some(0));
    assert_eq!(backend.sequence_button(Some(0)), Some("key"));
    assert_eq!(backend.sequence_button(Some(1)), None);
    assert_eq!(backend.touch_size, Some((640, 480)));
    assert!(backend.capabilities.touch);
    assert_eq!(backend.touch().unwrap(), "touch-input");
    hardware["board"]["runtime"]["button_sequence_ids"] = json!(["missing"]);
    assert!(Backend::from_hardware(&hardware).is_err());
    hardware = minimal();
    hardware["board"]["screen"] = json!({"width":240,"height":240});
    hardware["board"]["audio"] = json!(true);
    hardware["board"]["runtime"]["capabilities"] = json!({"screen":false,"audio":false});
    let disabled = Backend::from_hardware(&hardware).unwrap();
    assert!(!disabled.capabilities.screen && !disabled.capabilities.audio);
    hardware["board"]["uart_channels"] = json!([0, 2]);
    assert!(Backend::from_hardware(&hardware).is_err());
}

#[test]
fn builtin_boards_keep_frontend_controls_with_derived_runtime_routes() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let temp = tempfile::tempdir().unwrap();
    let catalog = Catalog::open(&root, temp.path()).unwrap();
    for board in ["arcs-mini", "arcs-evb"] {
        let backend = Backend::from_hardware(
            &json!({"board":catalog.boards[board], "chip":catalog.chips["ls2684"]}),
        )
        .unwrap();
        assert!(backend.capabilities.snapshot && backend.capabilities.usb_adb);
        assert!(
            backend.capabilities.audio && backend.capabilities.screen && backend.capabilities.wifi
        );
        assert_eq!(backend.capabilities.uart_channels, [0, 1, 2]);
        if board == "arcs-mini" {
            assert_eq!(
                backend.button("function").unwrap(),
                "x-lisa-button-0"
            );
            assert_eq!(backend.sequence_id("function").unwrap(), Some(0));
            assert!(backend.capabilities.download);
        } else {
            assert_eq!(backend.button("k3").unwrap(), "x-lisa-button-2");
            assert_eq!(backend.sequence_id("k3").unwrap(), Some(2));
            assert_eq!(backend.touch_size, Some((240, 320)));
        }
    }
}

#[test]
fn unavailable_board_cannot_create_storage() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().join("root");
    std::fs::create_dir_all(root.join("boards")).unwrap();
    std::fs::create_dir_all(root.join("chips")).unwrap();
    let mut hardware = minimal();
    hardware["board"]["chip"] = json!("test-chip");
    hardware["board"]["runtime"]["available"] = json!(false);
    hardware["board"]["runtime"]["unavailable_reason"] =
        json!("Flash persistence is not implemented");
    for (directory, section) in [("boards", "board"), ("chips", "chip")] {
        std::fs::write(
            root.join(directory).join("test.json"),
            hardware[section].to_string(),
        )
        .unwrap();
    }
    let catalog = Catalog::open(&root, &temp.path().join("library")).unwrap();
    let error = catalog.create("test-board", None, None).unwrap_err();
    assert_eq!(error.to_string(), "Flash persistence is not implemented");
    assert!(catalog.entries().unwrap().is_empty());
    assert!(Backend::for_device(&catalog, &json!({"hardware":hardware})).is_err());
}

#[test]
fn clock_quantum_uses_chip_metadata_and_rejects_invalid_values() {
    let mut hardware = minimal();
    hardware["chip"]["runtime"]["clock_quantum_ns"] = json!(10_000);
    let backend = Backend::from_hardware(&hardware).unwrap();
    let mut command = Command::new("qemu");
    backend
        .configure(
            &mut command,
            "device".as_ref(),
            "output".as_ref(),
            1,
            false,
            false,
        )
        .unwrap();
    assert!(
        command
            .get_envs()
            .any(|(key, value)| key == "LISEM_QEMU_SOC_CLOCK"
                && value == Some(std::ffi::OsStr::new("10000")))
    );
    for invalid in [json!(0), json!(-1), json!("10000")] {
        hardware["chip"]["runtime"]["clock_quantum_ns"] = invalid;
        assert!(Backend::from_hardware(&hardware).is_err());
    }
}
