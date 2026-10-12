//! Explicit raw terminal receiver, independent of a guest run or GUI polling.
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{
    fs::OpenOptions,
    io::{Read, Write},
    net::TcpStream,
    path::Path,
    sync::{Arc, Mutex, atomic::{AtomicBool, Ordering}},
    thread,
    time::{Duration, Instant},
};

pub struct Capture {
    stop: Arc<AtomicBool>,
    state: Arc<Mutex<Value>>,
    worker: Option<thread::JoinHandle<()>>,
}
impl Capture {
    pub fn start(endpoint: &str, path: &Path, timeout: u64) -> Result<Self> {
        ensure!((1..=172800).contains(&timeout), "UART capture deadline must be 1..172800 seconds");
        let mut input: Box<dyn Read + Send> = if let Some(address) = endpoint.strip_prefix("tcp://") {
            let stream = TcpStream::connect(address)?;
            stream.set_nonblocking(true)?;
            Box::new(stream)
        } else {
            #[cfg(unix)] {
                use std::os::unix::fs::OpenOptionsExt;
                Box::new(OpenOptions::new().read(true).write(true)
                    .custom_flags(libc::O_NONBLOCK | libc::O_NOCTTY).open(endpoint)?)
            }
            #[cfg(not(unix))] { anyhow::bail!("Unsupported UART endpoint"); }
        };
        let mut file = OpenOptions::new().write(true).create_new(true).open(path)
            .context("Cannot create UART capture")?;
        let state = Arc::new(Mutex::new(json!({"capture":uuid::Uuid::new_v4().simple().to_string(),
            "status":"running","path":path,"bytes":0,"timeout":timeout})));
        let progress = state.clone();
        let stop = Arc::new(AtomicBool::new(false));
        let cancel = stop.clone();
        let worker = thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(timeout);
            let mut bytes = [0; 16384];
            let mut total = 0u64;
            let result: Result<()> = (|| {
                while !cancel.load(Ordering::Acquire) && Instant::now() < deadline {
                    match input.read(&mut bytes) {
                        Ok(0) => anyhow::bail!("UART endpoint closed"),
                        Ok(n) => {
                            file.write_all(&bytes[..n])?;
                            total += n as u64;
                            progress.lock().unwrap()["bytes"] = json!(total);
                        }
                        Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => thread::sleep(Duration::from_millis(5)),
                        Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {},
                        Err(e) => return Err(e.into()),
                    }
                }
                file.flush()?;
                Ok(())
            })();
            let mut state = progress.lock().unwrap();
            state["status"] = json!(if result.is_err() { "failed" } else { "completed" });
            state["reason"] = json!(if cancel.load(Ordering::Acquire) { "requested" } else { "deadline" });
            if let Err(error) = result { state["error"] = json!(error.to_string()); }
        });
        Ok(Self { stop, state, worker: Some(worker) })
    }
    pub fn status(&self) -> Value { self.state.lock().unwrap().clone() }
    pub fn stop(&mut self) -> Value {
        self.stop.store(true, Ordering::Release);
        if let Some(worker) = self.worker.take() { let _ = worker.join(); }
        self.status()
    }
}
impl Drop for Capture { fn drop(&mut self) { self.stop(); } }

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn receiver_preserves_binary_stream_and_closes_independently() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("uart.bin");
        let mut receiver = Capture::start(&format!("tcp://{}", listener.local_addr().unwrap()), &path, 5).unwrap();
        let (mut sender, _) = listener.accept().unwrap();
        let expected: Vec<u8> = (0..200000).map(|i| i as u8).collect();
        sender.write_all(&expected).unwrap();
        let deadline = Instant::now() + Duration::from_secs(3);
        while receiver.status()["bytes"] != expected.len() {
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(5));
        }
        let status = receiver.stop();
        assert_eq!(status["status"], "completed");
        assert_eq!(std::fs::read(&path).unwrap(), expected);
        // Receiver does not own the UART endpoint or close the producer.
        assert!(sender.write_all(b"after capture").is_ok());
    }
}
