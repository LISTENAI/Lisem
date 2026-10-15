use lisem_core::{assets::Assets, storage};
use serde_json::json;
use std::{fs, path::Path, process::Command};

fn git(root: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(args)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap().trim().to_owned()
}

#[test]
fn checkout_manifest_covers_submodule_revision_worktree_and_external_assets() {
    let temporary = tempfile::tempdir().unwrap();
    let root = temporary.path();
    let source = root.join("qemu");
    fs::create_dir_all(&source).unwrap();
    fs::create_dir_all(root.join(".tools/qemu-build")).unwrap();
    fs::create_dir_all(root.join("tools")).unwrap();
    fs::create_dir_all(root.join("assets")).unwrap();
    git(&source, &["init", "-q"]);
    fs::write(source.join("source.c"), "source\n").unwrap();
    fs::write(source.join(".gitignore"), "build/\n").unwrap();
    git(&source, &["add", "."]);
    git(
        &source,
        &[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
    );
    let assets = Assets::new(root).unwrap();
    fs::write(assets.qemu(), "binary").unwrap();
    fs::write(root.join("tools/build_qemu.py"), "build").unwrap();
    fs::write(root.join("assets/rom.bin"), "rom").unwrap();
    let stamp = root.join(".tools/qemu-build/arcs-build.json");
    let manifest = json!({"binary_sha256":storage::sha256(&assets.qemu()).unwrap(),
        "qemu_revision":git(&source, &["rev-parse","HEAD"]),
        "qemu_worktree_sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "inputs_sha256":{"tools/build_qemu.py":storage::sha256(&root.join("tools/build_qemu.py")).unwrap(),
        "assets/rom.bin":storage::sha256(&root.join("assets/rom.bin")).unwrap()}});
    storage::write_json(&stamp, &manifest).unwrap();
    assets.verify_qemu().unwrap();
    fs::create_dir_all(source.join("build")).unwrap();
    fs::write(source.join("build/generated"), "ignored build output").unwrap();
    assets.verify_qemu().unwrap();
    fs::write(source.join("source.c"), "edited\n").unwrap();
    assert!(
        assets
            .verify_qemu()
            .unwrap_err()
            .to_string()
            .contains("source changed")
    );
    fs::write(source.join("source.c"), "source\n").unwrap();
    fs::write(source.join("new.c"), "new source\n").unwrap();
    assert!(assets.verify_qemu().is_err());
    fs::remove_file(source.join("new.c")).unwrap();
    fs::write(root.join("assets/new.bin"), "new asset").unwrap();
    assert!(assets.verify_qemu().is_err());
    fs::remove_file(root.join("assets/new.bin")).unwrap();
    fs::write(root.join("assets/rom.bin"), "different ROM").unwrap();
    assert!(assets.verify_qemu().is_err());
    fs::write(root.join("assets/rom.bin"), "rom").unwrap();
    assets.verify_qemu().unwrap();
    git(
        &source,
        &[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "next",
        ],
    );
    assert!(
        assets
            .verify_qemu()
            .unwrap_err()
            .to_string()
            .contains("revision changed")
    );
    fs::rename(source.join(".git"), root.join("saved-qemu-git")).unwrap();
    git(root, &["init", "-q"]);
    assert!(
        assets
            .verify_qemu()
            .unwrap_err()
            .to_string()
            .contains("not an initialized submodule")
    );
}
