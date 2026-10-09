//! Bounded control and raw UART transport. Serial draining is independent of
//! GUI polling and continues while a QMP command waits for QEMU's main loop.
use anyhow::{Result, bail, ensure};
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    fs::File,
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    path::Path,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

pub fn listener() -> Result<TcpListener> {
    let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0))?;
    listener.set_nonblocking(true)?;
    Ok(listener)
}
pub fn accept(listener: &TcpListener) -> Result<TcpStream> {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        match listener.accept() {
            Ok((stream, _)) => {
                stream.set_nodelay(true)?;
                return Ok(stream);
            }
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                ensure!(
                    Instant::now() < deadline,
                    "QEMU transport connection timed out"
                );
                std::thread::sleep(Duration::from_millis(2));
            }
            Err(e) => return Err(e.into()),
        }
    }
}

const UART_CAPACITY: usize = 1024 * 1024;
#[derive(Default)]
pub struct OutputBuffer {
    bytes: VecDeque<u8>,
    closed: bool,
    history: VecDeque<u8>,
    position: u64,
    tail: Option<(TcpStream, Option<File>)>,
}
impl OutputBuffer {
    fn observe(&mut self, bytes: &[u8]) {
        self.position += bytes.len() as u64;
        self.history.extend(bytes);
        let excess = self.history.len().saturating_sub(65536);
        self.history.drain(..excess);
    }
    pub fn read(&mut self, cursor: u64, limit: usize) -> Result<Value> {
        ensure!(
            (1..=16384).contains(&limit),
            "UART read limit must be 1..16384"
        );
        self.drain_tail()?;
        ensure!(cursor <= self.position, "UART cursor is beyond this run");
        let oldest = self.position - self.history.len() as u64;
        let start = cursor.max(oldest);
        let bytes: Vec<u8> = self
            .history
            .iter()
            .skip((start - oldest) as usize)
            .take(limit)
            .copied()
            .collect();
        Ok(
            json!({"cursor":start + bytes.len() as u64,"oldest":oldest,"available":self.position,
                  "lost":cursor < oldest,"hex":crate::storage::hex(&bytes),"text":String::from_utf8_lossy(&bytes)}),
        )
    }

    fn drain_tail(&mut self) -> Result<()> {
        if !self.closed {
            return Ok(());
        }
        let Some((stream, log)) = &mut self.tail else {
            return Ok(());
        };
        let mut buffer = [0; 65536];
        let limit = buffer.len().min(UART_CAPACITY - self.bytes.len());
        if limit == 0 {
            return Ok(());
        }
        match stream.read(&mut buffer[..limit]) {
            Ok(0) => self.tail = None,
            Ok(n) => {
                if let Some(log) = log {
                    log.write_all(&buffer[..n])?;
                }
                self.observe(&buffer[..n]);
                self.bytes.extend(&buffer[..n]);
            }
            Err(e) if e.kind() == std::io::ErrorKind::ConnectionReset => self.tail = None,
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
            Err(e) => return Err(e.into()),
        }
        Ok(())
    }
    fn exhausted(&self) -> bool {
        self.closed && self.tail.is_none() && self.bytes.is_empty()
    }
}
pub type Output = Arc<Mutex<OutputBuffer>>;

pub struct Uart {
    pub stream: TcpStream,
    pub pending: VecDeque<u8>,
    log: Option<File>,
    output: Output,
    retain: bool,
}
impl Uart {
    pub fn new(stream: TcpStream, path: Option<&Path>) -> Result<Self> {
        stream.set_nonblocking(true)?;
        let log = path.map(File::create).transpose()?;
        let tail = (
            stream.try_clone()?,
            log.as_ref().map(File::try_clone).transpose()?,
        );
        Ok(Self {
            stream,
            pending: VecDeque::new(),
            log,
            output: Arc::new(Mutex::new(OutputBuffer {
                tail: Some(tail),
                ..Default::default()
            })),
            retain: true,
        })
    }
    pub fn observer(&self) -> Output {
        self.output.clone()
    }
    pub fn output(&mut self) -> Output {
        self.retain = true;
        self.output.clone()
    }
    pub fn discard_output(&mut self) {
        self.retain = false;
        self.output.lock().unwrap().bytes.clear();
    }
    pub fn send(&mut self, bytes: &[u8]) -> Result<()> {
        ensure!(
            self.pending.len() + bytes.len() <= 65536,
            "UART input queue is full"
        );
        self.pending.extend(bytes);
        Ok(())
    }
    pub fn pump(&mut self) -> Result<()> {
        let mut buffer = [0; 65536];
        // Bound each host turn so a noisy UART cannot starve other channels.
        for _ in 0..16 {
            let mut output = self.output.lock().unwrap();
            let available = if self.retain {
                UART_CAPACITY - output.bytes.len()
            } else {
                buffer.len()
            };
            let limit = available.min(buffer.len());
            if limit == 0 {
                break;
            }
            match self.stream.read(&mut buffer[..limit]) {
                Ok(0) => break,
                Ok(n) => {
                    if let Some(log) = &mut self.log {
                        log.write_all(&buffer[..n])?;
                    }
                    output.observe(&buffer[..n]);
                    if self.retain {
                        output.bytes.extend(&buffer[..n]);
                    }
                }
                Err(e)
                    if matches!(
                        e.kind(),
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::ConnectionReset
                    ) =>
                {
                    break;
                }
                Err(e) => return Err(e.into()),
            }
        }
        if !self.pending.is_empty() {
            match self.stream.write(self.pending.make_contiguous()) {
                Ok(n) => {
                    self.pending.drain(..n);
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                Err(e) => return Err(e.into()),
            }
        }
        Ok(())
    }
}

impl Drop for Uart {
    fn drop(&mut self) {
        self.output.lock().unwrap().closed = true;
    }
}

const QMP_CAPACITY: usize = 2 * 1024 * 1024;
const QMP_TIMEOUT: Duration = Duration::from_secs(3);

fn write_control(
    writer: &mut impl Write,
    request: &[u8],
    offset: &mut usize,
    deadline: Instant,
    mut pump: impl FnMut() -> Result<()>,
) -> Result<()> {
    while *offset < request.len() {
        if Instant::now() >= deadline {
            return Err(ControlPending.into());
        }
        pump()?;
        let end = request.len().min(*offset + 65536);
        match writer.write(&request[*offset..end]) {
            Ok(0) => bail!("QEMU control connection closed during request"),
            Ok(written) => *offset += written,
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {}
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) =>
            {
                std::thread::sleep(Duration::from_millis(1));
            }
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

/// The command may still complete; it must never be retried implicitly.
#[derive(Debug)]
pub struct ControlPending;
impl std::fmt::Display for ControlPending {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("QEMU control response pending; outcome is not yet confirmed")
    }
}
impl std::error::Error for ControlPending {}

struct PendingControl {
    request: Vec<u8>,
    offset: usize,
    expires: Instant,
    recoverable: bool,
}

pub struct Qmp {
    stream: TcpStream,
    buffer: Vec<u8>,
    sequence: u64,
    pending: Option<PendingControl>,
}
impl Qmp {
    pub fn new(stream: TcpStream, uart: &mut [Uart]) -> Result<Self> {
        stream.set_read_timeout(Some(Duration::from_millis(10)))?;
        stream.set_write_timeout(Some(Duration::from_millis(10)))?;
        let mut qmp = Self {
            stream,
            buffer: Vec::new(),
            sequence: 0,
            pending: None,
        };
        ensure!(
            qmp.read(uart, Instant::now() + QMP_TIMEOUT)?
                .get("QMP")
                .is_some(),
            "Missing QMP greeting"
        );
        qmp.call("qmp_capabilities", json!({}), uart)?;
        Ok(qmp)
    }
    fn read(&mut self, uart: &mut [Uart], deadline: Instant) -> Result<Value> {
        loop {
            if let Some(end) = self.buffer.iter().position(|&b| b == b'\n') {
                let line: Vec<_> = self.buffer.drain(..=end).collect();
                return Ok(serde_json::from_slice(&line)?);
            }
            if Instant::now() >= deadline {
                return Err(ControlPending.into());
            }
            for port in &mut *uart {
                port.pump()?;
            }
            let mut bytes = [0; 65536];
            match self.stream.read(&mut bytes) {
                Ok(0) => bail!("QEMU control connection closed"),
                Ok(n) => self.buffer.extend_from_slice(&bytes[..n]),
                Err(e)
                    if matches!(
                        e.kind(),
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                    ) => {}
                Err(e) => return Err(e.into()),
            }
            ensure!(self.buffer.len() <= QMP_CAPACITY, "QMP response too large");
        }
    }
    pub fn ensure_idle(&self) -> Result<()> {
        ensure!(
            self.pending.is_none(),
            "Previous QEMU control command is still pending; new command was not sent"
        );
        Ok(())
    }
    pub fn pending_id(&self) -> Option<u64> {
        self.pending.as_ref().map(|_| self.sequence)
    }
    fn finish(&mut self, uart: &mut [Uart], deadline: Instant) -> Result<Value> {
        let pending = self.pending.as_mut().expect("pending control command");
        write_control(
            &mut self.stream,
            &pending.request,
            &mut pending.offset,
            deadline,
            || {
                for port in &mut *uart {
                    port.pump()?;
                }
                Ok(())
            },
        )?;
        loop {
            // Check even with buffered events: an event flood remains bounded.
            if Instant::now() >= deadline {
                return Err(ControlPending.into());
            }
            let reply = self.read(uart, deadline)?;
            if reply.get("event").is_some() {
                continue;
            }
            ensure!(reply["id"] == self.sequence, "Unexpected QMP response ID");
            self.pending = None;
            if let Some(error) = reply.get("error") {
                bail!("{}", error["desc"].as_str().unwrap_or("QMP error"));
            }
            return Ok(reply["return"].clone());
        }
    }
    /// Resume the original transaction without admitting another command.
    /// A timeout retains both the partial request and partial reply.
    pub fn recover(&mut self, uart: &mut [Uart]) -> Result<Option<(u64, Result<Value>)>> {
        let Some(pending) = &self.pending else {
            return Ok(None);
        };
        ensure!(
            pending.recoverable,
            "QEMU control mutation outcome is unknown; runtime must stop to release controls"
        );
        ensure!(
            Instant::now() < pending.expires,
            "QEMU control recovery timed out; command outcome remains unknown"
        );
        let id = self.sequence;
        let deadline = (Instant::now() + Duration::from_millis(10)).min(pending.expires);
        match self.finish(uart, deadline) {
            Err(error) if error.is::<ControlPending>() => Ok(None),
            result if self.pending.is_none() => Ok(Some((id, result))),
            Err(error) => Err(error),
            Ok(_) => unreachable!(),
        }
    }
    pub fn call(&mut self, method: &str, args: Value, uart: &mut [Uart]) -> Result<Value> {
        self.call_until(method, args, uart, Instant::now() + QMP_TIMEOUT)
    }
    fn call_until(
        &mut self,
        method: &str,
        args: Value,
        uart: &mut [Uart],
        deadline: Instant,
    ) -> Result<Value> {
        self.ensure_idle()?;
        let sequence = self.sequence + 1;
        let mut request =
            serde_json::to_vec(&json!({"execute":method,"arguments":args,"id":sequence}))?;
        request.push(b'\n');
        ensure!(request.len() <= QMP_CAPACITY, "QMP request too large");
        self.sequence = sequence;
        self.pending = Some(PendingControl {
            request,
            offset: 0,
            expires: Instant::now() + Duration::from_secs(30),
            recoverable: method == "qom-get"
                || (method == "qom-set" && args["property"] == "x-lisa-camera-frame"),
        });
        let result = self.finish(uart, deadline);
        if let Err(error) = &result
            && !error.is::<ControlPending>()
            && self.pending.is_some()
        {
            // A broken connection/protocol cannot safely accept later commands.
            let _ = self.stream.shutdown(std::net::Shutdown::Both);
        }
        result
    }
    pub fn get(&mut self, name: &str, uart: &mut [Uart]) -> Result<Value> {
        self.call("qom-get", json!({"path":"/machine","property":name}), uart)
    }
    pub fn set(&mut self, name: &str, value: Value, uart: &mut [Uart]) -> Result<()> {
        self.call(
            "qom-set",
            json!({"path":"/machine","property":name,"value":value}),
            uart,
        )?;
        Ok(())
    }
}

/// A stable endpoint for an instance/channel, including while powered off.
/// POSIX uses a PTY; Windows exposes a local raw TCP terminal without a driver.
pub struct SerialPort {
    pub path: String,
    #[cfg(unix)]
    master: File,
    #[cfg(windows)]
    master: SocketTerminal,
    #[cfg(unix)]
    _slave: File,
    sources: VecDeque<Output>,
    pending: Vec<u8>,
}
impl SerialPort {
    pub fn new() -> Result<Self> {
        #[cfg(unix)]
        {
            use std::os::fd::FromRawFd;
            let (mut master, mut slave) = (-1, -1);
            let mut name = [0 as libc::c_char; 1024];
            ensure!(
                unsafe {
                    libc::openpty(
                        &mut master,
                        &mut slave,
                        name.as_mut_ptr(),
                        std::ptr::null_mut(),
                        std::ptr::null_mut(),
                    )
                } == 0,
                "Cannot open PTY: {}",
                std::io::Error::last_os_error()
            );
            let master_file = unsafe { File::from_raw_fd(master) };
            let slave_file = unsafe { File::from_raw_fd(slave) };
            let mut mode = std::mem::MaybeUninit::uninit();
            ensure!(
                unsafe { libc::tcgetattr(slave, mode.as_mut_ptr()) } == 0,
                "Cannot read PTY settings"
            );
            let mut mode = unsafe { mode.assume_init() };
            unsafe { libc::cfmakeraw(&mut mode) };
            ensure!(
                unsafe { libc::tcsetattr(slave, libc::TCSANOW, &mode) } == 0,
                "Cannot configure PTY"
            );
            ensure!(
                unsafe { libc::fcntl(master, libc::F_SETFL, libc::O_NONBLOCK) } >= 0,
                "Cannot set PTY nonblocking mode"
            );
            for fd in [master, slave] {
                ensure!(
                    unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) } >= 0,
                    "Cannot protect PTY descriptor"
                );
            }
            Ok(Self {
                path: unsafe { std::ffi::CStr::from_ptr(name.as_ptr()) }
                    .to_str()?
                    .into(),
                master: master_file,
                _slave: slave_file,
                sources: VecDeque::new(),
                pending: Vec::new(),
            })
        }
        #[cfg(windows)]
        {
            let master = SocketTerminal {
                listener: listener()?,
                stream: None,
            };
            Ok(Self {
                path: format!("tcp://{}", master.listener.local_addr()?),
                master,
                sources: VecDeque::new(),
                pending: Vec::new(),
            })
        }
    }
    pub fn bind(&mut self, output: Output) -> Result<()> {
        self.sources.retain(|source| {
            let source = source.lock().unwrap();
            !source.exhausted()
        });
        ensure!(
            self.sources.len() < 8,
            "UART output backlog spans too many runs; drain the terminal"
        );
        self.sources.push_back(output);
        self.discard_input();
        Ok(())
    }
    fn discard_input(&mut self) {
        #[cfg(unix)]
        {
            use std::os::fd::AsRawFd;
            unsafe { libc::tcflush(self._slave.as_raw_fd(), libc::TCOFLUSH) };
        }
        #[cfg(windows)]
        {
            let mut bytes = [0; 4096];
            for _ in 0..16 {
                match self.master.read(&mut bytes) {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {}
                }
            }
        }
        self.pending.clear();
    }
    pub fn pump(&mut self, uart: Option<&mut Uart>) -> Result<()> {
        {
            if let Some(source) = self.sources.front() {
                let mut output = source.lock().unwrap();
                output.drain_tail()?;
                let bytes = output.bytes.make_contiguous();
                let length = bytes.len().min(4096);
                if length != 0 {
                    match self.master.write(&bytes[..length]) {
                        Ok(n) => {
                            output.bytes.drain(..n);
                        }
                        Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                        Err(e) => return Err(e.into()),
                    }
                }
                let exhausted = output.exhausted();
                drop(output);
                if exhausted {
                    self.sources.pop_front();
                }
            }
            if let Some(uart) = uart {
                if self.pending.is_empty() {
                    let mut bytes = [0; 4096];
                    match self.master.read(&mut bytes) {
                        Ok(n) => self.pending.extend_from_slice(&bytes[..n]),
                        Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                        Err(e) => return Err(e.into()),
                    }
                }
                if uart.pending.len() + self.pending.len() <= 65536 {
                    uart.send(&self.pending)?;
                    self.pending.clear();
                }
            } else {
                self.discard_input();
            }
        }
        Ok(())
    }
}

#[cfg(windows)]
struct SocketTerminal {
    listener: TcpListener,
    stream: Option<TcpStream>,
}
#[cfg(windows)]
impl SocketTerminal {
    fn transfer(
        &mut self,
        action: impl FnOnce(&mut TcpStream) -> std::io::Result<usize>,
    ) -> std::io::Result<usize> {
        if self.stream.is_none() {
            let (stream, _) = self.listener.accept()?;
            stream.set_nonblocking(true)?;
            stream.set_nodelay(true)?;
            self.stream = Some(stream);
        }
        let result = action(self.stream.as_mut().unwrap());
        match result {
            Ok(0) => {}
            Err(ref e)
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::ConnectionReset
                        | std::io::ErrorKind::BrokenPipe
                        | std::io::ErrorKind::ConnectionAborted
                ) => {}
            _ => return result,
        }
        self.stream = None;
        Err(std::io::ErrorKind::WouldBlock.into())
    }
}
#[cfg(windows)]
impl Read for SocketTerminal {
    fn read(&mut self, bytes: &mut [u8]) -> std::io::Result<usize> {
        self.transfer(|stream| stream.read(bytes))
    }
}
#[cfg(windows)]
impl Write for SocketTerminal {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.transfer(|stream| stream.write(bytes))
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(unix)]
    use std::fs::OpenOptions;
    #[cfg(unix)]
    use std::os::unix::fs::OpenOptionsExt;

    #[test]
    fn qmp_wait_drains_uart_and_accepts_events_and_fragmented_replies() {
        use std::io::{BufRead, BufReader};
        let qmp_listener = listener().unwrap();
        let uart_listener = listener().unwrap();
        let expected: Vec<u8> = (0..256)
            .cycle()
            .take(1024 * 1024)
            .map(|v| v as u8)
            .collect();
        let sent = expected.clone();
        let qmp_address = qmp_listener.local_addr().unwrap();
        let uart_address = uart_listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let mut qmp = TcpStream::connect(qmp_address).unwrap();
            let mut serial = TcpStream::connect(uart_address).unwrap();
            serial
                .set_write_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            writeln!(qmp, "{{\"QMP\":{{}}}}").unwrap();
            let mut line = String::new();
            BufReader::new(qmp.try_clone().unwrap())
                .read_line(&mut line)
                .unwrap();
            let request: Value = serde_json::from_str(&line).unwrap();
            serial.write_all(&sent).unwrap();
            writeln!(qmp, "{{\"event\":\"RESET\"}}").unwrap();
            let reply = format!("{}\n", json!({"return":{},"id":request["id"]}));
            qmp.write_all(&reply.as_bytes()[..5]).unwrap();
            qmp.write_all(&reply.as_bytes()[5..]).unwrap();
        });
        let temporary = tempfile::tempdir().unwrap();
        let log = temporary.path().join("uart.bin");
        let mut uart = [Uart::new(accept(&uart_listener).unwrap(), Some(&log)).unwrap()];
        Qmp::new(accept(&qmp_listener).unwrap(), &mut uart).unwrap();
        server.join().unwrap();
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            uart[0].pump().unwrap();
            if std::fs::metadata(&log).unwrap().len() == expected.len() as u64
                || Instant::now() >= deadline
            {
                break;
            }
            std::thread::sleep(Duration::from_millis(1));
        }
        assert_eq!(std::fs::read(log).unwrap(), expected);
    }

    #[test]
    fn control_write_resumes_short_writes_and_transient_errors_with_one_deadline() {
        struct Fragmented {
            bytes: Vec<u8>,
            attempts: usize,
        }
        impl Write for Fragmented {
            fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
                self.attempts += 1;
                match self.attempts % 5 {
                    1 => Err(std::io::ErrorKind::WouldBlock.into()),
                    2 => Err(std::io::ErrorKind::TimedOut.into()),
                    3 => Err(std::io::ErrorKind::Interrupted.into()),
                    _ => {
                        let count = bytes.len().min(71);
                        self.bytes.extend_from_slice(&bytes[..count]);
                        Ok(count)
                    }
                }
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        let mut writer = Fragmented {
            bytes: Vec::new(),
            attempts: 0,
        };
        let expected: Vec<_> = (0..900).map(|i| i as u8).collect();
        let mut pumps = 0;
        write_control(
            &mut writer,
            &expected,
            &mut 0,
            Instant::now() + Duration::from_secs(1),
            || {
                pumps += 1;
                Ok(())
            },
        )
        .unwrap();
        assert_eq!(writer.bytes, expected);
        assert_eq!(pumps, writer.attempts);
        let error =
            write_control(&mut writer, b"later", &mut 0, Instant::now(), || Ok(())).unwrap_err();
        assert!(error.to_string().contains("outcome is not yet confirmed"));
        assert_eq!(writer.bytes, expected);
        let mut offset = 123;
        let mut resumed = Fragmented {
            bytes: expected[..offset].to_vec(),
            attempts: 0,
        };
        assert!(
            write_control(&mut resumed, &expected, &mut offset, Instant::now(), || Ok(
                ()
            ))
            .is_err()
        );
        assert_eq!(offset, 123);
        write_control(
            &mut resumed,
            &expected,
            &mut offset,
            Instant::now() + Duration::from_secs(1),
            || Ok(()),
        )
        .unwrap();
        assert_eq!(resumed.bytes, expected);
        assert_eq!(offset, expected.len());

        struct Blocked;
        impl Write for Blocked {
            fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
                Err(std::io::ErrorKind::WouldBlock.into())
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        let start = Instant::now();
        assert!(
            write_control(
                &mut Blocked,
                b"blocked",
                &mut 0,
                start + Duration::from_millis(20),
                || Ok(())
            )
            .is_err()
        );
        assert!(start.elapsed() < Duration::from_secs(1));
    }

    #[test]
    fn qmp_large_frame_survives_tcp_backpressure_while_draining_uart() {
        use std::io::{BufRead, BufReader};
        let qmp_listener = listener().unwrap();
        let uart_listener = listener().unwrap();
        let qmp_address = qmp_listener.local_addr().unwrap();
        let uart_address = uart_listener.local_addr().unwrap();
        let pixels = "A".repeat(640 * 480 * 4);
        let expected = pixels.clone();
        let serial_bytes: Vec<_> = (0..512 * 1024).map(|i| (i * 31) as u8).collect();
        let sent = serial_bytes.clone();
        let server = std::thread::spawn(move || {
            let mut qmp = TcpStream::connect(qmp_address).unwrap();
            qmp.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
            qmp.set_write_timeout(Some(Duration::from_secs(3))).unwrap();
            let mut serial = TcpStream::connect(uart_address).unwrap();
            serial
                .set_write_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            writeln!(qmp, "{{\"QMP\":{{}}}}").unwrap();
            let mut line = String::new();
            BufReader::new(qmp.try_clone().unwrap())
                .read_line(&mut line)
                .unwrap();
            let request: Value = serde_json::from_str(&line).unwrap();
            writeln!(qmp, "{}", json!({"return":{},"id":request["id"]})).unwrap();
            // Hold off control reads while UART output becomes available, then
            // consume the frame in fragments. A blocked writer must still pump UART.
            serial.write_all(&sent).unwrap();
            std::thread::sleep(Duration::from_millis(50));
            let mut frame = Vec::new();
            while !frame.ends_with(b"\n") {
                let mut chunk = [0; 4096];
                let count = qmp.read(&mut chunk).unwrap();
                assert!(count > 0);
                frame.extend_from_slice(&chunk[..count]);
                assert!(frame.len() <= QMP_CAPACITY);
                std::thread::sleep(Duration::from_micros(100));
            }
            let request: Value = serde_json::from_slice(&frame).unwrap();
            assert_eq!(request["execute"], "qom-set");
            assert_eq!(request["arguments"]["property"], "x-lisa-camera-frame");
            assert_eq!(request["arguments"]["value"], expected);
            writeln!(qmp, "{{\"event\":\"RESET\"}}").unwrap();
            let reply = format!("{}\n", json!({"return":{},"id":request["id"]}));
            qmp.write_all(&reply.as_bytes()[..3]).unwrap();
            qmp.write_all(&reply.as_bytes()[3..]).unwrap();
        });
        let temporary = tempfile::tempdir().unwrap();
        let log = temporary.path().join("uart.bin");
        let mut uart = [Uart::new(accept(&uart_listener).unwrap(), Some(&log)).unwrap()];
        let mut qmp = Qmp::new(accept(&qmp_listener).unwrap(), &mut uart).unwrap();
        #[cfg(unix)]
        {
            use std::os::fd::AsRawFd;
            let size: libc::c_int = 8192;
            assert_eq!(
                unsafe {
                    libc::setsockopt(
                        qmp.stream.as_raw_fd(),
                        libc::SOL_SOCKET,
                        libc::SO_SNDBUF,
                        &size as *const _ as *const libc::c_void,
                        std::mem::size_of_val(&size) as libc::socklen_t,
                    )
                },
                0
            );
        }
        qmp.set("x-lisa-camera-frame", json!(pixels), &mut uart)
            .unwrap();
        server.join().unwrap();
        let deadline = Instant::now() + Duration::from_secs(3);
        while std::fs::metadata(&log).unwrap().len() < serial_bytes.len() as u64 {
            assert!(Instant::now() < deadline);
            uart[0].pump().unwrap();
        }
        assert_eq!(std::fs::read(log).unwrap(), serial_bytes);
    }

    #[test]
    fn qmp_recovers_late_partial_reply_and_rejection_without_resending() {
        use std::io::{BufRead, BufReader};
        for rejected in [false, true] {
            let listener = listener().unwrap();
            let address = listener.local_addr().unwrap();
            let server = std::thread::spawn(move || {
                let mut peer = TcpStream::connect(address).unwrap();
                peer.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
                let mut reader = BufReader::new(peer.try_clone().unwrap());
                writeln!(peer, "{{\"QMP\":{{}}}}").unwrap();
                for command in 0..3 {
                    let mut line = String::new();
                    reader.read_line(&mut line).unwrap();
                    let request: Value = serde_json::from_str(&line).unwrap();
                    let reply = if command == 1 && rejected {
                        json!({"id":request["id"],"error":{"desc":"test rejection"}})
                    } else {
                        json!({"id":request["id"],"return":command})
                    };
                    let reply = format!("{reply}\n");
                    if command == 1 {
                        writeln!(peer, "{{\"event\":\"RESET\"}}").unwrap();
                        peer.write_all(&reply.as_bytes()[..5]).unwrap();
                        std::thread::sleep(Duration::from_millis(100));
                        peer.write_all(&reply.as_bytes()[5..]).unwrap();
                    } else {
                        peer.write_all(reply.as_bytes()).unwrap();
                    }
                }
            });
            let mut qmp = Qmp::new(accept(&listener).unwrap(), &mut []).unwrap();
            let error = qmp
                .call_until(
                    "qom-set",
                    json!({"property":"x-lisa-camera-frame"}),
                    &mut [],
                    Instant::now() + Duration::from_millis(30),
                )
                .unwrap_err();
            assert!(error.is::<ControlPending>());
            let id = qmp.pending_id().unwrap();
            assert!(
                qmp.call("must-not-send", json!({}), &mut [])
                    .unwrap_err()
                    .to_string()
                    .contains("not sent")
            );
            let completed = loop {
                if let Some(completed) = qmp.recover(&mut []).unwrap() {
                    break completed;
                }
            };
            assert_eq!(completed.0, id);
            if rejected {
                assert_eq!(completed.1.unwrap_err().to_string(), "test rejection");
            } else {
                assert_eq!(completed.1.unwrap(), 1);
            }
            assert_eq!(qmp.call("next", json!({}), &mut []).unwrap(), 2);
            server.join().unwrap();
        }
    }

    #[test]
    fn qmp_resumes_partial_camera_request_and_bounds_recovery() {
        use std::io::{BufRead, BufReader};
        let listener = listener().unwrap();
        let address = listener.local_addr().unwrap();
        let pixels = "A".repeat(640 * 480 * 4);
        let expected = pixels.clone();
        let server = std::thread::spawn(move || {
            let mut peer = TcpStream::connect(address).unwrap();
            peer.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
            let mut reader = BufReader::new(peer.try_clone().unwrap());
            writeln!(peer, "{{\"QMP\":{{}}}}").unwrap();
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            let hello: Value = serde_json::from_str(&line).unwrap();
            writeln!(peer, "{}", json!({"id":hello["id"],"return":{}})).unwrap();
            std::thread::sleep(Duration::from_millis(100));
            line.clear();
            reader.read_line(&mut line).unwrap();
            let frame: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(frame["arguments"]["value"], expected);
            writeln!(peer, "{}", json!({"id":frame["id"],"return":{}})).unwrap();
        });
        let mut qmp = Qmp::new(accept(&listener).unwrap(), &mut []).unwrap();
        #[cfg(unix)]
        {
            use std::os::fd::AsRawFd;
            let size: libc::c_int = 8192;
            assert_eq!(
                unsafe {
                    libc::setsockopt(
                        qmp.stream.as_raw_fd(),
                        libc::SOL_SOCKET,
                        libc::SO_SNDBUF,
                        &size as *const _ as *const libc::c_void,
                        std::mem::size_of_val(&size) as libc::socklen_t,
                    )
                },
                0
            );
        }
        assert!(
            qmp.call_until(
                "qom-set",
                json!({"property":"x-lisa-camera-frame","value":pixels}),
                &mut [],
                Instant::now() + Duration::from_millis(30)
            )
            .unwrap_err()
            .is::<ControlPending>()
        );
        let pending = qmp.pending.as_ref().unwrap();
        assert!(pending.offset > 0);
        #[cfg(unix)]
        assert!(pending.offset < pending.request.len());
        while qmp.recover(&mut []).unwrap().is_none() {}
        server.join().unwrap();
        assert!(qmp.pending_id().is_none());
        qmp.pending = Some(PendingControl {
            request: vec![],
            offset: 0,
            expires: Instant::now(),
            recoverable: true,
        });
        assert!(
            qmp.recover(&mut [])
                .unwrap_err()
                .to_string()
                .contains("outcome remains unknown")
        );
        qmp.pending.as_mut().unwrap().expires = Instant::now() + Duration::from_secs(30);
        qmp.pending.as_mut().unwrap().recoverable = false;
        assert!(
            qmp.recover(&mut [])
                .unwrap_err()
                .to_string()
                .contains("runtime must stop to release controls")
        );
    }

    #[test]
    fn observation_is_bounded_cursor_based_and_does_not_consume_terminal_data() {
        let mut output = OutputBuffer::default();
        let bytes: Vec<u8> = (0..70000).map(|v| v as u8).collect();
        output.bytes.extend(&bytes);
        for chunk in bytes.chunks(4096) {
            output.observe(chunk);
        }
        assert_eq!(output.history.len(), 65536);
        let first = output.read(0, 100).unwrap();
        assert_eq!(first["oldest"], 4464);
        assert_eq!(first["lost"], true);
        assert_eq!(first["cursor"], 4564);
        assert_eq!(
            crate::storage::unhex(first["hex"].as_str().unwrap()).unwrap(),
            bytes[4464..4564]
        );
        assert_eq!(first, output.read(0, 100).unwrap());
        assert_eq!(output.bytes.len(), 70000);
        assert_eq!(output.read(4564, 100).unwrap()["lost"], false);
        assert!(output.read(70001, 10).is_err());
        assert!(output.read(0, 16385).is_err());
    }

    #[test]
    fn retired_uart_drains_socket_tail_without_exceeding_memory_budget() {
        let listener = listener().unwrap();
        let mut peer = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
        peer.set_write_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let mut uart = Uart::new(accept(&listener).unwrap(), None).unwrap();
        let output = uart.output();
        let expected: Vec<u8> = (0..UART_CAPACITY + 65536).map(|i| (i * 31) as u8).collect();
        let sent = expected.clone();
        let writer = std::thread::spawn(move || peer.write_all(&sent).unwrap());
        let deadline = Instant::now() + Duration::from_secs(5);
        while output.lock().unwrap().bytes.len() < UART_CAPACITY {
            uart.pump().unwrap();
            assert!(Instant::now() < deadline);
            std::thread::yield_now();
        }
        uart.pump().unwrap();
        assert_eq!(output.lock().unwrap().bytes.len(), UART_CAPACITY);
        drop(uart);
        let mut actual = Vec::new();
        loop {
            let mut buffer = output.lock().unwrap();
            buffer.drain_tail().unwrap();
            assert!(buffer.bytes.len() <= UART_CAPACITY);
            actual.extend(buffer.bytes.drain(..));
            if buffer.exhausted() {
                break;
            }
            assert!(Instant::now() < deadline);
            drop(buffer);
            std::thread::yield_now();
        }
        writer.join().unwrap();
        assert_eq!(actual, expected);
    }

    #[test]
    fn terminal_backpressure_retains_raw_bytes_across_runs() {
        let temp = tempfile::tempdir().unwrap();
        let mut port = SerialPort::new().unwrap();
        #[cfg(unix)]
        let mut terminal = OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(&port.path)
            .unwrap();
        #[cfg(windows)]
        let mut terminal = {
            let stream = TcpStream::connect(port.path.strip_prefix("tcp://").unwrap()).unwrap();
            stream.set_nonblocking(true).unwrap();
            stream.set_nodelay(true).unwrap();
            stream
        };
        let first: Vec<u8> = (0..131072).map(|i| (i * 73) as u8).collect();
        let second: Vec<u8> = (0..65536).map(|i| (i * 19 + 7) as u8).collect();
        let a = Arc::new(Mutex::new(OutputBuffer {
            bytes: first.clone().into(),
            closed: true,
            tail: None,
            ..Default::default()
        }));
        let b = Arc::new(Mutex::new(OutputBuffer {
            bytes: second.clone().into(),
            closed: true,
            tail: None,
            ..Default::default()
        }));
        port.bind(a).unwrap();
        // Fill the terminal without reading, then seal the run. Unread output
        // must survive the next run and the powered-off input purge.
        for _ in 0..64 {
            port.pump(None).unwrap();
        }
        port.bind(b).unwrap();
        let expected: Vec<u8> = first.into_iter().chain(second).collect();
        let mut actual = Vec::new();
        let deadline = Instant::now() + Duration::from_secs(5);
        while actual.len() < expected.len() && Instant::now() < deadline {
            port.pump(None).unwrap();
            let mut bytes = [0; 4096];
            match terminal.read(&mut bytes) {
                Ok(n) => actual.extend_from_slice(&bytes[..n]),
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                Err(e) => panic!("{e}"),
            }
            std::thread::sleep(Duration::from_micros(100));
        }
        assert_eq!(actual.len(), expected.len());
        assert_eq!(actual, expected);

        let listener = listener().unwrap();
        let mut peer = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
        peer.set_nonblocking(true).unwrap();
        let mut uart = Uart::new(
            accept(&listener).unwrap(),
            Some(&temp.path().join("raw.bin")),
        )
        .unwrap();
        uart.pending.resize(65536, 42);
        let input: Vec<u8> = (0..=255).collect();
        terminal.write_all(&input).unwrap();
        port.pump(Some(&mut uart)).unwrap();
        assert_eq!(port.pending, input);
        port.pump(Some(&mut uart)).unwrap();
        assert_eq!(port.pending, input);
        // Drain the congested socket before accepting the retained PTY input.
        let mut received = Vec::new();
        let deadline = Instant::now() + Duration::from_secs(5);
        while received.len() < 65536 + input.len() && Instant::now() < deadline {
            uart.pump().unwrap();
            port.pump(Some(&mut uart)).unwrap();
            let mut bytes = [0; 8192];
            match peer.read(&mut bytes) {
                Ok(n) => received.extend_from_slice(&bytes[..n]),
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                Err(e) => panic!("{e}"),
            }
            std::thread::sleep(Duration::from_micros(100));
        }
        assert_eq!(&received[..65536], vec![42; 65536]);
        assert_eq!(&received[65536..], input);
    }
}
