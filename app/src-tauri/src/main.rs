// Keep the binary thin so the reusable pieces stay in the library, which is what the
// mobile entry point and the tests both use.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    stacksearch_app_lib::run();
}
