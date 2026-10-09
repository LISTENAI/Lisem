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

#[test]
fn attached_libraries_observe_worker_build_without_replacing_it() {
    use serde_json::Value;
    use std::{
        io::{BufRead, BufReader, ErrorKind, Write},
        net::TcpListener,
        thread,
        time::{Duration, Instant},
    };

    let temp = tempfile::tempdir().unwrap();
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let mut first = Manager::new(
        &root,
        &temp.path().join("first"),
        Path::new("must-not-spawn"),
    )
    .unwrap();
    let item = first.call("create", json!({"board":"arcs-mini"})).unwrap();
    let path = Path::new(item["path"].as_str().unwrap());
    let mut second = Manager::new(
        &root,
        &temp.path().join("second"),
        Path::new("must-not-spawn"),
    )
    .unwrap();
    second.call("attach", json!({"path":path})).unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    storage::write_json(
        &path.join("runtime.json"),
        &json!({"version":1,"instance_id":item["id"],
        "port":listener.local_addr().unwrap().port(),"token":"test-token"}),
    )
    .unwrap();
    let mut old_build = lisem_core::identity::build();
    old_build["source_sha256"] = json!("earlier source snapshot");
    let worker = json!({"id":"existing-worker","build":old_build,"pid":123});
    let thread = thread::spawn(move || {
        // connect() probes status before inspect/status asks for it again.
        for index in 0..8 {
            let deadline = Instant::now() + Duration::from_secs(5);
            let mut stream = loop {
                match listener.accept() {
                    Ok((stream, _)) => break stream,
                    Err(error) if error.kind() == ErrorKind::WouldBlock => {
                        assert!(Instant::now() < deadline, "Worker observation timed out");
                        thread::sleep(Duration::from_millis(5));
                    }
                    Err(error) => panic!("Worker observation failed: {error}"),
                }
            };
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            stream
                .set_write_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut request = String::new();
            BufReader::new(stream.try_clone().unwrap())
                .read_line(&mut request)
                .unwrap();
            let request: Value = serde_json::from_str(&request).unwrap();
            assert_eq!(
                request["method"], "status",
                "Observation must not stop or restart the worker"
            );
            assert_eq!(request["token"], "test-token");
            let response = if index < 4 {
                json!({"worker":worker,"session":null,"serial":{}})
            } else {
                json!({"session":null,"serial":{}})
            };
            writeln!(stream, "{}", json!({"result":response})).unwrap();
        }
    });
    for manager in [&mut first, &mut second] {
        let status = manager.call("inspect", json!({"id":item["id"]})).unwrap();
        assert_eq!(status["client_worker_build"], "different");
        assert_eq!(status["runtime"]["worker"]["id"], "existing-worker");
    }
    for manager in [&first, &second] {
        let status = manager.status().unwrap();
        assert_eq!(
            status["runtimes"][item["id"].as_str().unwrap()]["client_worker_build"],
            "unknown"
        );
    }
    thread.join().unwrap();
}

#[test]
fn camera_input_selection_is_validated_persisted_and_explicitly_cleared() {
    use lisem_core::runtime::{Options, Runtime};
    let temp = tempfile::tempdir().unwrap();
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let data = temp.path().join("library");
    let catalog = Catalog::open(&root, &data).unwrap();
    let item = catalog.create("arcs-mini", None, None).unwrap();
    let id = item["id"].as_str().unwrap().to_owned();
    let mut runtime = Runtime::new(Catalog::open(&root, &data).unwrap(), id.clone()).unwrap();
    let image_path = temp.path().join("camera.png");
    image::RgbImage::from_pixel(4, 3, image::Rgb([1, 2, 3]))
        .save(&image_path)
        .unwrap();
    let params = json!({"path":image_path});
    assert!(runtime.call("camera", &json!({"path":true})).is_err());
    assert!(runtime.call("camera", &json!({})).is_err());
    assert!(
        runtime
            .call("camera", &json!({"path":image_path,"run":"stale"}))
            .is_err()
    );
    assert_eq!(
        runtime.call("camera", &params).unwrap()["path"],
        json!(image_path)
    );
    assert_eq!(
        catalog.device(&id).unwrap()["host"]["camera_image"],
        json!(image_path)
    );
    assert!(
        runtime
            .call("camera", &json!({"path":temp.path().join("missing.png")}))
            .is_err()
    );
    assert_eq!(
        catalog.device(&id).unwrap()["host"]["camera_image"],
        json!(image_path)
    );
    fs::remove_file(&image_path).unwrap();
    let error = runtime
        .call("start", &json!({"options":Options::default()}))
        .unwrap_err();
    assert!(error.to_string().contains("Cannot open camera image"));
    runtime.call("camera", &json!({"path":null})).unwrap();
    assert!(catalog.device(&id).unwrap()["host"]["camera_image"].is_null());
    assert!(
        runtime
            .call("camera", &json!({"path":null,"device_id":"test"}))
            .is_err()
    );
    assert!(runtime.call("camera", &json!({"device_id":""})).is_err());
    if cfg!(target_os = "macos") {
        let selected = runtime
            .call("camera", &json!({"device_id":"test-device"}))
            .unwrap();
        assert_eq!(selected["status"], "saved");
        assert_eq!(
            catalog.device(&id).unwrap()["host"]["camera_device"],
            "test-device"
        );
        runtime.call("camera", &json!({"path":null})).unwrap();
        assert!(catalog.device(&id).unwrap()["host"]["camera_device"].is_null());
    } else {
        assert!(
            runtime
                .call("camera", &json!({"device_id":"test-device"}))
                .unwrap_err()
                .to_string()
                .contains("unsupported")
        );
    }
}
