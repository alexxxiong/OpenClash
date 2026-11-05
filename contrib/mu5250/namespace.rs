use std::ffi::CString;
use std::fs;
use std::os::raw::{c_char, c_int, c_ulong};
use std::os::unix::io::AsRawFd;

unsafe extern "C" {
    fn unshare(flags: c_int) -> c_int;
    fn setns(fd: c_int, nstype: c_int) -> c_int;
    fn mount(source: *const c_char, target: *const c_char, fstype: *const c_char,
             flags: c_ulong, data: *const c_char) -> c_int;
    fn execvp(file: *const c_char, argv: *const *const c_char) -> c_int;
    fn umask(mask: u32) -> u32;
}

fn c(value: &str) -> CString { CString::new(value).expect("NUL in argument") }
fn check(result: c_int, label: &str) {
    if result != 0 { panic!("{label}: {}", std::io::Error::last_os_error()); }
}

fn main() {
    unsafe { umask(0o077); }
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 4 { panic!("usage: oc-namespace new ROOT COMMAND... | enter PID COMMAND..."); }
    if args[1] == "new" {
        check(unsafe { unshare(0x00020000) }, "unshare mount namespace");
        check(unsafe { mount(std::ptr::null(), c("/").as_ptr(), std::ptr::null(), 16384 | 262144, std::ptr::null()) }, "make mounts private");
        for (target, name) in [("/usr", "usr"), ("/www", "www"), ("/etc/init.d", "init.d")] {
            let upper = format!("{}/upper/{name}", args[2]);
            let work = format!("{}/work/{name}", args[2]);
            fs::create_dir_all(&upper).expect("upper directory");
            fs::create_dir_all(&work).expect("work directory");
            let options = format!("lowerdir={target},upperdir={upper},workdir={work}");
            check(unsafe { mount(c("overlay").as_ptr(), c(target).as_ptr(), c("overlay").as_ptr(), 0, c(&options).as_ptr()) }, target);
        }
        fs::create_dir_all(format!("{}/run", args[2])).expect("run directory");
        fs::write(format!("{}/run/namespace.pid", args[2]), std::process::id().to_string()).expect("namespace pid");
    } else if args[1] == "enter" {
        let pid: u32 = args[2].parse().expect("namespace PID");
        let file = fs::File::open(format!("/proc/{pid}/ns/mnt")).expect("open namespace");
        check(unsafe { setns(file.as_raw_fd(), 0x00020000) }, "enter mount namespace");
    } else { panic!("unknown mode"); }
    let command: Vec<CString> = args[3..].iter().map(|arg| c(arg)).collect();
    let mut pointers: Vec<*const c_char> = command.iter().map(|arg| arg.as_ptr()).collect();
    pointers.push(std::ptr::null());
    unsafe { execvp(command[0].as_ptr(), pointers.as_ptr()); }
    panic!("exec: {}", std::io::Error::last_os_error());
}
