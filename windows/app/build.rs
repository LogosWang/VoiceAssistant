// The pages are the same ones the Mac app shows, so they are copied in at build time, not kept twice.
use std::{fs, path::Path};

fn copy_dir(from: &Path, to: &Path) {
    fs::create_dir_all(to).unwrap();
    for entry in fs::read_dir(from).unwrap() {
        let entry = entry.unwrap();
        let target = to.join(entry.file_name());
        if entry.file_type().unwrap().is_dir() {
            copy_dir(&entry.path(), &target);
        } else {
            fs::copy(entry.path(), target).unwrap();
        }
    }
}

fn main() {
    let dist = Path::new("dist");
    let shared = Path::new("../../app");
    copy_dir(&shared.join("art"), &dist.join("art"));
    for page in ["index.html", "settings.html"] {
        fs::copy(shared.join(page), dist.join(page)).unwrap();
        println!("cargo:rerun-if-changed=../../app/{page}");
    }
    fs::copy("pages/rename.html", dist.join("rename.html")).unwrap();
    println!("cargo:rerun-if-changed=pages/rename.html");
    tauri_build::build()
}
