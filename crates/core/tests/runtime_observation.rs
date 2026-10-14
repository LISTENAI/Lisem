use lisem_core::{manager::Manager, storage};
use serde_json::{Value, json};
use std::{
    io::{BufRead, BufReader, Write},
    net::TcpListener,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

struct Worker {
    paused: Arc<AtomicBool>,
    requests: Arc<AtomicUsize>,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}
impl Worker {
    fn start(path: &Path, id: &str) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        storage::write_json(
            &path.join("runtime.json"),
            &json!({"version":1,
            "instance_id":id,"port":listener.local_addr().unwrap().port(),"token":"fixture"}),
        )
        .unwrap();
        let paused = Arc::new(AtomicBool::new(false));
        let stop = Arc::new(AtomicBool::new(false));
        let requests = Arc::new(AtomicUsize::new(0));
        let accepted = requests.clone();
        let halted = paused.clone();
        let done = stop.clone();
        let worker = thread::spawn(move || {
            let mut held = Vec::new();
            let mut count = 0;
            while !done.load(Ordering::Acquire) {
                if let Ok((mut stream, _)) = listener.accept() {
                    accepted.fetch_add(1, Ordering::AcqRel);
                    stream.set_nonblocking(false).unwrap();
                    if halted.load(Ordering::Acquire) {
                        held.push(stream);
                        continue;
                    }
                    stream
                        .set_read_timeout(Some(Duration::from_millis(500)))
                        .unwrap();
                    let mut line = String::new();
                    BufReader::new(stream.try_clone().unwrap())
                        .read_line(&mut line)
                        .unwrap();
                    let request: Value = serde_json::from_str(&line).unwrap();
                    assert_eq!(request["method"], "status");
                    assert_eq!(request["token"], "fixture");
                    count += 1;
                    writeln!(
                        stream,
                        "{}",
                        json!({"result":{"session":{"output":"run",
                        "finished":false,"sample":count},"serial":{},"worker":{}}})
                    )
                    .unwrap();
                } else {
                    thread::sleep(Duration::from_millis(2));
                }
            }
        });
        Self {
            paused,
            requests,
            stop,
            thread: Some(worker),
        }
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        let result = self.thread.take().unwrap().join();
        if !thread::panicking() {
            result.unwrap();
        }
    }
}

#[test]
fn unresponsive_instance_preserves_its_observation_without_blocking_other_instances() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let temp = tempfile::tempdir().unwrap();
    let mut manager = Manager::new(&root, temp.path(), Path::new("must-not-spawn")).unwrap();
    let a = manager
        .catalog
        .create("arcs-mini", None, Some("a"))
        .unwrap();
    let b = manager
        .catalog
        .create("arcs-mini", None, Some("b"))
        .unwrap();
    let aid = a["id"].as_str().unwrap();
    let bid = b["id"].as_str().unwrap();
    let first = Worker::start(Path::new(a["path"].as_str().unwrap()), aid);
    let _second = Worker::start(Path::new(b["path"].as_str().unwrap()), bid);
    let initial = manager.status().unwrap();
    assert_eq!(initial["sessions"][aid]["sample"], 1);
    assert_eq!(initial["sessions"][bid]["sample"], 1);
    first.paused.store(true, Ordering::Release);
    let started = Instant::now();
    let delayed = manager.status().unwrap();
    assert!(started.elapsed() < Duration::from_secs(3));
    assert_eq!(delayed["sessions"][aid]["sample"], 1);
    assert_eq!(delayed["sessions"][bid]["sample"], 2);
    assert_eq!(delayed["runtimes"][aid]["stale"], true);
    assert!(delayed["runtime_errors"][aid].is_string());
    assert!(delayed["runtime_errors"][bid].is_null());
    let error = manager.call("start", json!({"id":aid})).unwrap_err();
    assert!(error.to_string().contains("not responding"));
    first.paused.store(false, Ordering::Release);
    let recovered = manager.status().unwrap();
    assert_eq!(recovered["sessions"][aid]["sample"], 2);
    assert_eq!(recovered["runtimes"][aid]["stale"], false);
    assert!(recovered["runtime_errors"][aid].is_null());
}

#[test]
fn many_unresponsive_instances_use_bounded_fair_observation_rounds() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let temp = tempfile::tempdir().unwrap();
    let manager = Manager::new(&root, temp.path(), Path::new("must-not-spawn")).unwrap();
    let mut workers = Vec::new();
    for index in 0..20 {
        let item = manager.catalog.create("arcs-mini", None, Some(&format!("fixture {index}"))).unwrap();
        let worker = Worker::start(Path::new(item["path"].as_str().unwrap()), item["id"].as_str().unwrap());
        worker.paused.store(true, Ordering::Release);
        workers.push(worker);
    }
    let mut previous = 0;
    for _ in 0..3 {
        let started = Instant::now();
        let status = manager.status().unwrap();
        assert!(started.elapsed() < Duration::from_secs(2));
        assert_eq!(status["runtimes"].as_object().unwrap().len(), 20);
        let total: usize = workers.iter().map(|w| w.requests.load(Ordering::Acquire)).sum();
        assert_eq!(total - previous, 8, "each round has at most eight concurrent requests");
        previous = total;
    }
    assert!(workers.iter().all(|w| w.requests.load(Ordering::Acquire) >= 1), "rotation must visit every worker");
}
