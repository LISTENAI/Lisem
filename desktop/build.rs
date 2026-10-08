fn main() {
    println!("cargo:rerun-if-changed=assets/app/lisem.ico");
    println!("cargo:rerun-if-changed=assets/windows.rc");
    #[cfg(windows)]
    embed_resource::compile_for(
        "assets/windows.rc",
        &["lisem-desktop"],
        embed_resource::ParamsIncludeDirs(["assets"]),
    )
    .manifest_optional()
    .expect("Failed to embed the application icon");
}
