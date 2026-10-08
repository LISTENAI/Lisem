use std::{
    path::Path,
    process::{Command, Output},
};

fn cli(data: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_lisem"))
        .arg("--root")
        .arg(Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."))
        .arg("--data-dir")
        .arg(data)
        .args(args)
        .output()
        .unwrap()
}
#[test]
fn default_is_readable_and_json_remains_scriptable() {
    let directory = tempfile::tempdir().unwrap();
    let created = cli(
        directory.path(),
        &[
            "--json",
            "create",
            "--board",
            "arcs-mini",
            "--name",
            "Test Mini",
        ],
    );
    assert!(
        created.status.success(),
        "{}",
        String::from_utf8_lossy(&created.stderr)
    );
    let item: serde_json::Value = serde_json::from_slice(&created.stdout).unwrap();
    let id = item["id"].as_str().unwrap();
    let listed = cli(directory.path(), &["list"]);
    let text = String::from_utf8(listed.stdout).unwrap();
    assert!(text.contains(id) && text.contains("Test Mini") && text.contains("BOARD"));
    assert!(!text.contains("hardware") && !text.starts_with('['));
    let listed = cli(directory.path(), &["list", "--json"]);
    let list: serde_json::Value = serde_json::from_slice(&listed.stdout).unwrap();
    assert_eq!(list[0]["uid"], item["uid"]);
    let uid = cli(directory.path(), &["uid", id]);
    assert_eq!(
        String::from_utf8(uid.stdout).unwrap().trim(),
        item["uid"].as_str().unwrap()
    );
    let status = cli(directory.path(), &["status", id]);
    assert!(
        String::from_utf8(status.stdout)
            .unwrap()
            .contains("Power: Off")
    );
    assert!(
        !cli(directory.path(), &["status", "missing"])
            .status
            .success()
    );
}
