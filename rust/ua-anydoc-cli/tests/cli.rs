use std::fs;
use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT_TEMP_ID: AtomicU64 = AtomicU64::new(0);

fn anydoc() -> Command {
    Command::new(env!("CARGO_BIN_EXE_anydoc"))
}

fn temp_dir(label: &str) -> PathBuf {
    let id = NEXT_TEMP_ID.fetch_add(1, Ordering::Relaxed);
    let path =
        std::env::temp_dir().join(format!("ua-anydoc-cli-{label}-{}-{id}", std::process::id()));
    fs::create_dir_all(&path).expect("create test directory");
    path
}

fn write_pdf_fixture(label: &str, contents: &[u8]) -> (PathBuf, PathBuf) {
    let directory = temp_dir(label);
    let input = directory.join("fixture.pdf");
    fs::write(&input, contents).expect("write PDF fixture");
    (directory, input)
}

#[test]
fn converts_csv_file_to_stdout() {
    let directory = temp_dir("file");
    let input = directory.join("scores.csv");
    fs::write(&input, "name,score\nAda,10\n").expect("write csv");

    let result = anydoc().arg(&input).output().expect("run anydoc");

    assert!(
        result.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&result.stderr)
    );
    let markdown = String::from_utf8(result.stdout).expect("utf8 markdown");
    assert!(markdown.contains("Ada"));
    assert!(markdown.contains("score"));
    fs::remove_dir_all(directory).expect("remove test directory");
}

#[test]
fn converts_explicit_stdin_format_to_output_file() {
    let directory = temp_dir("stdin");
    let output = directory.join("scores.md");
    let mut child = anydoc()
        .args(["-", "--format", "csv", "--output"])
        .arg(&output)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn anydoc");
    child
        .stdin
        .take()
        .expect("stdin pipe")
        .write_all(b"name,score\nGrace,11\n")
        .expect("write stdin");

    let result = child.wait_with_output().expect("wait for anydoc");

    assert!(
        result.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&result.stderr)
    );
    assert!(result.stdout.is_empty());
    let markdown = fs::read_to_string(&output).expect("read markdown");
    assert!(markdown.contains("Grace"));
    fs::remove_dir_all(directory).expect("remove test directory");
}

#[test]
fn distinguishes_usage_and_conversion_failures() {
    let usage = anydoc()
        .args(["-", "--format", "yaml"])
        .output()
        .expect("run usage failure");
    assert_eq!(usage.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&usage.stderr).starts_with("anydoc: invalid format"));

    let conversion = anydoc()
        .arg("this-file-does-not-exist.docx")
        .output()
        .expect("run conversion failure");
    assert_eq!(conversion.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&conversion.stderr).starts_with("anydoc: "));
}

#[test]
fn deeply_nested_pdf_fails_without_aborting_the_process() {
    const NESTING_DEPTH: usize = 10_500;

    let mut pdf = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /X ".to_vec();
    pdf.extend(std::iter::repeat_n(b'[', NESTING_DEPTH));
    pdf.extend(std::iter::repeat_n(b']', NESTING_DEPTH));
    pdf.extend_from_slice(b" >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n");
    let (directory, input) = write_pdf_fixture("deeply-nested-pdf", &pdf);

    let result = anydoc()
        .args(["--format", "pdf"])
        .arg(&input)
        .output()
        .expect("run anydoc");

    assert_eq!(result.status.code(), Some(1), "process must exit normally");
    assert!(
        String::from_utf8_lossy(&result.stderr).starts_with("anydoc:"),
        "unexpected stderr: {}",
        String::from_utf8_lossy(&result.stderr)
    );
    fs::remove_dir_all(directory).expect("remove test directory");
}

#[test]
fn malformed_pdf_returns_a_conversion_error() {
    let (directory, input) = write_pdf_fixture(
        "malformed-pdf",
        b"%PDF-1.4\nthis is not a valid PDF object\n%%EOF\n",
    );

    let result = anydoc()
        .args(["--format", "pdf"])
        .arg(&input)
        .output()
        .expect("run anydoc");

    assert_eq!(result.status.code(), Some(1));
    assert!(
        String::from_utf8_lossy(&result.stderr).starts_with("anydoc:"),
        "unexpected stderr: {}",
        String::from_utf8_lossy(&result.stderr)
    );
    fs::remove_dir_all(directory).expect("remove test directory");
}

#[test]
fn reports_upstream_aligned_version_and_help() {
    let version = anydoc().arg("--version").output().expect("run version");
    assert!(version.status.success());
    assert_eq!(version.stdout, b"0.1.9\n");

    let help = anydoc().arg("--help").output().expect("run help");
    assert!(help.status.success());
    let help_text = String::from_utf8(help.stdout).expect("utf8 help");
    assert!(help_text.contains("anydoc <file> [options]"));
    assert!(help_text.contains("Never prompts"));
}
