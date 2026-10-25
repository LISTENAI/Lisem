use sha2::{Digest, Sha256};
use std::{env, fs, path::Path};

fn sources(root: &Path, path: &Path, files: &mut Vec<String>) {
    println!("cargo:rerun-if-changed={}", path.display());
    for entry in fs::read_dir(path).expect("Read build source directory") {
        let path = entry.expect("Read build source entry").path();
        if path.is_dir() {
            sources(root, &path, files);
        } else if path.extension().is_some_and(|s| s == "rs")
            || path.file_name().is_some_and(|s| s == "Cargo.toml")
        {
            files.push(
                path.strip_prefix(root)
                    .unwrap()
                    .to_str()
                    .unwrap()
                    .replace('\\', "/"),
            );
        }
    }
}

fn main() {
    let manifest = env::var("CARGO_MANIFEST_DIR").unwrap();
    let root = Path::new(&manifest).parent().unwrap().parent().unwrap();
    let mut files = vec!["Cargo.toml".to_owned(), "Cargo.lock".to_owned()];
    sources(root, &root.join("crates"), &mut files);
    sources(root, &root.join("desktop/src"), &mut files);
    files.push("desktop/Cargo.toml".into());
    files.push("desktop/build.rs".into());
    files.sort();
    let mut digest = Sha256::new();
    for file in files {
        let path = root.join(&file);
        println!("cargo:rerun-if-changed={}", path.display());
        digest.update(file.as_bytes());
        digest.update([0]);
        let bytes = fs::read(path).expect("Read build identity input");
        digest.update((bytes.len() as u64).to_le_bytes());
        digest.update(bytes);
    }
    println!(
        "cargo:rustc-env=LISEM_SOURCE_SHA256={:x}",
        digest.finalize()
    );
    println!(
        "cargo:rustc-env=LISEM_BUILD_TARGET={}",
        env::var("TARGET").unwrap()
    );
    let mut sdk_files = vec!["python/pyproject.toml".to_owned()];
    for directory in ["python/src/lisem", "python/bootstrap"] {
        let directory = root.join(directory);
        println!("cargo:rerun-if-changed={}", directory.display());
        for entry in fs::read_dir(directory).expect("Read SDK source directory") {
            let path = entry.expect("Read SDK source entry").path();
            if path.is_file() && (path.extension().is_some_and(|s| s == "py")
                || path.file_name().is_some_and(|s| s == "py.typed")) {
                sdk_files.push(path.strip_prefix(root).unwrap().to_str().unwrap().replace('\\', "/"));
            }
        }
    }
    sdk_files.sort();
    let mut sdk_digest = Sha256::new();
    for file in sdk_files {
        println!("cargo:rerun-if-changed={}", root.join(&file).display());
        let bytes = fs::read(root.join(&file)).expect("Read SDK identity input");
        sdk_digest.update(file.as_bytes()); sdk_digest.update([0]);
        sdk_digest.update((bytes.len() as u64).to_le_bytes()); sdk_digest.update(bytes);
    }
    println!("cargo:rustc-env=LISEM_PYTHON_SHA256={:x}",sdk_digest.finalize());
}
