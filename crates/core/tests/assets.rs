use lisem_core::{assets::Assets, storage};
use serde_json::json;
use std::fs;

#[test]
fn relocated_bundle_checks_native_assets_and_library_integrity() {
    let temp = tempfile::tempdir().unwrap();
    let contents = temp.path().join("Lisem.app/Contents");
    let root = contents.join("Resources/runtime");
    fs::create_dir_all(root.join("bin")).unwrap();
    fs::create_dir_all(root.join("lib")).unwrap();
    fs::create_dir_all(contents.join("Frameworks")).unwrap();
    let root = root.canonicalize().unwrap();
    // Use each platform's asset names without executing these fixture files.
    storage::write_json(&root.join("manifest.json"), &json!({})).unwrap();
    let assets = Assets::new(&root).unwrap();
    let mut files = serde_json::Map::new();
    for path in [assets.qemu(), assets.audio(), assets.network()] {
        fs::write(&path, b"native asset").unwrap();
        files.insert(
            path.strip_prefix(&root)
                .unwrap()
                .to_string_lossy()
                .replace('\\', "/"),
            json!(storage::sha256(&path).unwrap()),
        );
    }
    let library = if cfg!(target_os = "macos") {
        contents.join("Frameworks/dependency.dylib")
    } else {
        root.join("lib/dependency.so")
    };
    fs::write(&library, b"library").unwrap();
    if !cfg!(target_os = "macos") {
        files.insert(
            "lib/dependency.so".into(),
            json!(storage::sha256(&library).unwrap()),
        );
    }
    let manifest = json!({"version":1,"files":files,"host_files":{},
        "frameworks":{"dependency.dylib":storage::sha256(&library).unwrap()}});
    storage::write_json(&root.join("manifest.json"), &manifest).unwrap();
    assets.verify_qemu().unwrap();
    let moved = temp.path().join("Moved.app");
    fs::rename(contents.parent().unwrap(), &moved).unwrap();
    let root = moved.join("Contents/Resources/runtime");
    let assets = Assets::new(&root).unwrap();
    assets.verify_qemu().unwrap();
    let library = if cfg!(target_os = "macos") {
        moved.join("Contents/Frameworks/dependency.dylib")
    } else {
        root.join("lib/dependency.so")
    };
    fs::write(&library, b"changed").unwrap();
    assert!(assets.verify_qemu().is_err());
    fs::write(&library, b"library").unwrap();
    for name in ["../outside", "/absolute"] {
        let mut invalid = manifest.clone();
        invalid["files"][name] = json!("invalid");
        storage::write_json(&root.join("manifest.json"), &invalid).unwrap();
        assert!(assets.verify_qemu().is_err());
    }
    storage::write_json(&root.join("manifest.json"), &manifest).unwrap();
    fs::remove_file(assets.qemu()).unwrap();
    assert!(assets.verify_qemu().is_err());
    #[cfg(unix)]
    {
        let outside = temp.path().join("outside");
        fs::write(&outside, b"native asset").unwrap();
        std::os::unix::fs::symlink(&outside, assets.qemu()).unwrap();
        assert!(assets.verify_qemu().is_err());
    }
}
