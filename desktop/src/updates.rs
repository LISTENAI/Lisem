use semver::Version;
use serde::Deserialize;
use std::time::Duration;

const ENDPOINT: &str = "https://api.github.com/repos/LISTENAI/Lisem/releases/latest";
const BODY_LIMIT: u64 = 1024 * 1024;

#[derive(Debug, PartialEq)]
pub enum ReleaseStatus {
    Current,
    Available { version: Version, url: String },
    Unpublished,
}

#[derive(Debug, PartialEq)]
pub enum CheckError {
    Network,
    Timeout,
    RateLimited,
    Http(u16),
    InvalidRelease,
}

impl CheckError {
    pub fn message(&self) -> String {
        match self {
            Self::Network => "无法连接 GitHub，请重试".into(),
            Self::Timeout => "检查超时，请重试".into(),
            Self::RateLimited => "请求过于频繁，请稍后重试".into(),
            Self::Http(status) => format!("检查失败（HTTP {status}），请重试"),
            Self::InvalidRelease => "版本信息无效，请稍后重试".into(),
        }
    }
}

#[derive(Deserialize)]
struct Release {
    tag_name: String,
    draft: bool,
    prerelease: bool,
}

fn compare(body: &[u8], current: &str) -> Result<ReleaseStatus, CheckError> {
    let release: Release = serde_json::from_slice(body).map_err(|_| CheckError::InvalidRelease)?;
    let tag = release
        .tag_name
        .strip_prefix('v')
        .unwrap_or(&release.tag_name);
    let version = Version::parse(tag).map_err(|_| CheckError::InvalidRelease)?;
    let current = Version::parse(current).map_err(|_| CheckError::InvalidRelease)?;
    if release.draft || release.prerelease || !version.pre.is_empty() {
        return Err(CheckError::InvalidRelease);
    }
    if version.cmp_precedence(&current).is_gt() {
        Ok(ReleaseStatus::Available {
            url: format!(
                "https://github.com/LISTENAI/Lisem/releases/tag/{}",
                release.tag_name
            ),
            version,
        })
    } else {
        Ok(ReleaseStatus::Current)
    }
}

fn transport_error(error: ureq::Error) -> CheckError {
    log::warn!("Release check failed: {error}");
    match error {
        ureq::Error::Timeout(_) => CheckError::Timeout,
        ureq::Error::BodyExceedsLimit(_) => CheckError::InvalidRelease,
        _ => CheckError::Network,
    }
}

fn fetch(agent: &ureq::Agent, endpoint: &str, current: &str) -> Result<ReleaseStatus, CheckError> {
    let mut response = agent
        .get(endpoint)
        .header("Accept", "application/vnd.github+json")
        .header("X-GitHub-Api-Version", "2022-11-28")
        .header("User-Agent", concat!("Lisem/", env!("CARGO_PKG_VERSION")))
        .call()
        .map_err(transport_error)?;
    match response.status().as_u16() {
        200 => {}
        404 => return Ok(ReleaseStatus::Unpublished),
        403 if response
            .headers()
            .get("x-ratelimit-remaining")
            .is_some_and(|v| v == "0")
            || response.headers().contains_key("retry-after") =>
        {
            return Err(CheckError::RateLimited);
        }
        429 => return Err(CheckError::RateLimited),
        status => return Err(CheckError::Http(status)),
    }
    let bytes = response
        .body_mut()
        .with_config()
        .limit(BODY_LIMIT)
        .read_to_vec()
        .map_err(transport_error)?;
    compare(&bytes, current)
}

fn agent(timeout: Duration) -> ureq::Agent {
    ureq::Agent::config_builder()
        .timeout_global(Some(timeout))
        .http_status_as_error(false)
        .max_redirects(0)
        .build()
        .into()
}

pub fn check() -> Result<ReleaseStatus, CheckError> {
    fetch(
        &agent(Duration::from_secs(10)),
        ENDPOINT,
        env!("CARGO_PKG_VERSION"),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        io::{Read, Write},
        net::TcpListener,
        thread,
    };

    fn release(tag: &str) -> Vec<u8> {
        serde_json::to_vec(
            &serde_json::json!({"tag_name": tag, "draft": false, "prerelease": false}),
        )
        .unwrap()
    }

    #[test]
    fn compares_version_precedence_without_downgrading() {
        assert!(matches!(
            compare(&release("v0.10.0"), "0.9.0"),
            Ok(ReleaseStatus::Available { .. })
        ));
        assert_eq!(
            compare(&release("v0.6.0"), "0.6.0"),
            Ok(ReleaseStatus::Current)
        );
        assert_eq!(
            compare(&release("v0.5.0"), "0.6.0"),
            Ok(ReleaseStatus::Current)
        );
        assert_eq!(
            compare(&release("v0.6.0+release"), "0.6.0+dev"),
            Ok(ReleaseStatus::Current)
        );
        assert!(matches!(
            compare(&release("0.6.0"), "0.6.0-rc.1"),
            Ok(ReleaseStatus::Available { .. })
        ));
        assert_eq!(
            compare(&release("v0.7.0-rc.1"), "0.6.0"),
            Err(CheckError::InvalidRelease)
        );
        assert_eq!(
            compare(&release("../../example"), "0.6.0"),
            Err(CheckError::InvalidRelease)
        );
        for flag in ["draft", "prerelease"] {
            let mut body: serde_json::Value = serde_json::from_slice(&release("v1.0.0")).unwrap();
            body[flag] = true.into();
            assert_eq!(
                compare(&serde_json::to_vec(&body).unwrap(), "0.6.0"),
                Err(CheckError::InvalidRelease)
            );
        }
    }

    fn serve(status: &str, headers: &str, body: &[u8]) -> (String, thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let response = format!(
            "HTTP/1.1 {status}\r\nContent-Length: {}\r\n{headers}Connection: close\r\n\r\n",
            body.len()
        )
        .into_bytes();
        let body = body.to_vec();
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut request = Vec::new();
            let mut byte = [0];
            while !request.ends_with(b"\r\n\r\n") {
                stream.read_exact(&mut byte).unwrap();
                request.push(byte[0]);
            }
            let request = String::from_utf8(request).unwrap().to_lowercase();
            assert!(request.contains("user-agent: lisem/"));
            assert!(request.contains("accept: application/vnd.github+json"));
            stream.write_all(&response).unwrap();
            let _ = stream.write_all(&body);
        });
        (url, handle)
    }

    #[test]
    fn handles_release_http_errors_and_untrusted_responses() {
        let client = agent(Duration::from_secs(2));
        for (status, headers, body, expected) in [
            (
                "200 OK",
                "",
                release("v0.7.0"),
                Ok(ReleaseStatus::Available {
                    version: Version::new(0, 7, 0),
                    url: "https://github.com/LISTENAI/Lisem/releases/tag/v0.7.0".into(),
                }),
            ),
            ("404 Not Found", "", vec![], Ok(ReleaseStatus::Unpublished)),
            (
                "403 Forbidden",
                "X-RateLimit-Remaining: 0\r\n",
                vec![],
                Err(CheckError::RateLimited),
            ),
            (
                "429 Too Many Requests",
                "",
                vec![],
                Err(CheckError::RateLimited),
            ),
            (
                "500 Internal Server Error",
                "",
                vec![],
                Err(CheckError::Http(500)),
            ),
            (
                "302 Found",
                "Location: https://example.org/\r\n",
                vec![],
                Err(CheckError::Http(302)),
            ),
            (
                "200 OK",
                "",
                b"not json".to_vec(),
                Err(CheckError::InvalidRelease),
            ),
        ] {
            let (url, handle) = serve(status, headers, &body);
            assert_eq!(fetch(&client, &url, "0.6.0"), expected);
            handle.join().unwrap();
        }
        let (url, handle) = serve("200 OK", "", &vec![b' '; BODY_LIMIT as usize + 1]);
        assert!(fetch(&client, &url, "0.6.0").is_err());
        handle.join().unwrap();
    }

    #[test]
    fn times_out_while_reading_release_body() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
                .unwrap();
            thread::sleep(Duration::from_millis(500));
        });
        assert_eq!(
            fetch(&agent(Duration::from_millis(100)), &url, "0.6.0"),
            Err(CheckError::Timeout)
        );
        server.join().unwrap();
    }
}
