//! Non-interactive CLI for the Rust `anydoc` document-conversion library.
//!
//! Upstream publishes its supported CLI through npm while the Rust crate is a
//! library. This wrapper keeps the official container path Rust-only and
//! preserves the documented CLI contract used by the bundled agent skill.

use std::ffi::{OsStr, OsString};
use std::fs;
use std::io::{self, IsTerminal, Read, Write};
use std::path::PathBuf;
use std::process::ExitCode;

use anydoc::Format;

const CONVERSION_ERROR: u8 = 1;
const USAGE_ERROR: u8 = 2;
const FORMATS: &str = "doc, docx, odt, pdf, ppt, pptx, rtf, epub, xlsx, ods, odp, csv";
const HELP: &str = "anydoc: convert documents to GitHub-Flavored Markdown

Usage:
  anydoc <file> [options]
  anydoc - [options] < file

Converts one document per invocation and writes the Markdown to stdout.
Pass - as the input to read the document from stdin. Never prompts; all
diagnostics go to stderr.

Options:
  -o, --output <path>    Write the Markdown to <path> instead of stdout
  -f, --format <format>  Name the input format instead of detecting it:
                         doc, docx, odt, pdf, ppt, pptx, rtf, epub,
                         xlsx, ods, odp, csv
                         (extension aliases like xls, docm, and ppsx resolve
                         to these)
  -h, --help             Print this help and exit
  -V, --version          Print the version and exit

The format is detected from the file content; the file extension is the
fallback for signature-less formats such as CSV. stdin has no extension, so
CSV input from stdin needs --format csv. Scanned or image-only PDFs need OCR,
which anydoc does not do, and error as unsupported.

Exit codes:
  0  success
  1  the document could not be read or converted
  2  usage error: unknown option, missing input, or invalid --format

Examples:
  anydoc report.docx
  anydoc slides.pptx -o slides.md
  anydoc - --format csv < data.csv
";

#[derive(Debug, PartialEq, Eq)]
struct Options {
    input: OsString,
    output: Option<PathBuf>,
    format: Option<Format>,
}

#[derive(Debug, PartialEq, Eq)]
enum ParseOutcome {
    Convert(Options),
    Help,
    Version,
}

fn main() -> ExitCode {
    let options = match parse_args(std::env::args_os().skip(1).collect()) {
        Ok(ParseOutcome::Convert(options)) => options,
        Ok(ParseOutcome::Help) => {
            print_stdout(HELP.as_bytes());
            return ExitCode::SUCCESS;
        }
        Ok(ParseOutcome::Version) => {
            print_stdout(concat!(env!("CARGO_PKG_VERSION"), "\n").as_bytes());
            return ExitCode::SUCCESS;
        }
        Err(message) => return fail(USAGE_ERROR, &message),
    };

    match convert(options) {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => fail(CONVERSION_ERROR, &message),
    }
}

fn parse_args(args: Vec<OsString>) -> Result<ParseOutcome, String> {
    let mut input = None;
    let mut output = None;
    let mut format = None;
    let mut positional_only = false;
    let mut index = 0;

    while index < args.len() {
        let argument = &args[index];
        let text = argument.to_string_lossy();
        if positional_only || argument == OsStr::new("-") || !text.starts_with('-') {
            if input.replace(argument.clone()).is_some() {
                return Err(format!(
                    "one document per invocation: unexpected second input '{}'",
                    argument.to_string_lossy()
                ));
            }
            index += 1;
            continue;
        }

        if argument == OsStr::new("--") {
            positional_only = true;
            index += 1;
            continue;
        }
        if matches!(text.as_ref(), "-h" | "--help") {
            return Ok(ParseOutcome::Help);
        }
        if matches!(text.as_ref(), "-V" | "--version") {
            return Ok(ParseOutcome::Version);
        }

        let (option, inline_value) = match text
            .strip_prefix("--")
            .and_then(|long| long.split_once('='))
        {
            Some((name, value)) => (format!("--{name}"), Some(OsString::from(value))),
            None => (text.into_owned(), None),
        };
        match option.as_str() {
            "-o" | "--output" => {
                let value = option_value(&args, &mut index, &option, inline_value)?;
                output = Some(PathBuf::from(value));
            }
            "-f" | "--format" => {
                let value = option_value(&args, &mut index, &option, inline_value)?;
                let value_text = value.to_string_lossy();
                format = Some(Format::from_extension(&value_text).ok_or_else(|| {
                    format!("invalid format '{value_text}'; expected one of: {FORMATS}")
                })?);
            }
            _ => return Err(format!("unknown option '{option}' (see anydoc --help)")),
        }
        index += 1;
    }

    let input = input.ok_or_else(|| {
        "missing input: pass a document path, or - for stdin (see anydoc --help)".to_string()
    })?;
    Ok(ParseOutcome::Convert(Options {
        input,
        output,
        format,
    }))
}

fn option_value(
    args: &[OsString],
    index: &mut usize,
    option: &str,
    inline_value: Option<OsString>,
) -> Result<OsString, String> {
    if let Some(value) = inline_value {
        return Ok(value);
    }
    *index += 1;
    args.get(*index)
        .cloned()
        .ok_or_else(|| format!("{option} requires a value"))
}

fn convert(options: Options) -> Result<(), String> {
    let markdown = if options.input == OsStr::new("-") {
        if io::stdin().is_terminal() {
            return Err("stdin is a terminal; pipe or redirect a document into anydoc -".into());
        }
        let mut bytes = Vec::new();
        io::stdin()
            .read_to_end(&mut bytes)
            .map_err(|error| error.to_string())?;
        anydoc::to_markdown_bytes(&bytes, options.format).map_err(|error| error.to_string())?
    } else {
        let path = PathBuf::from(&options.input);
        match options.format {
            Some(format) => {
                let bytes = fs::read(&path).map_err(|error| error.to_string())?;
                anydoc::to_markdown_bytes(&bytes, format).map_err(|error| error.to_string())?
            }
            None => anydoc::to_markdown(&path).map_err(|error| error.to_string())?,
        }
    };

    match options.output {
        Some(path) => fs::write(path, markdown).map_err(|error| error.to_string()),
        None => write_stdout(markdown.as_bytes()),
    }
}

fn write_stdout(bytes: &[u8]) -> Result<(), String> {
    match io::stdout().write_all(bytes) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::BrokenPipe => Ok(()),
        Err(error) => Err(error.to_string()),
    }
}

fn print_stdout(bytes: &[u8]) {
    // Help/version output is best effort, matching ordinary Unix CLI behavior.
    let _ = write_stdout(bytes);
}

fn fail(code: u8, message: &str) -> ExitCode {
    let _ = writeln!(io::stderr(), "anydoc: {message}");
    ExitCode::from(code)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_explicit_format_and_output() {
        assert_eq!(
            parse_args(vec![
                "report.data".into(),
                "--format=csv".into(),
                "-o".into(),
                "report.md".into(),
            ]),
            Ok(ParseOutcome::Convert(Options {
                input: "report.data".into(),
                output: Some("report.md".into()),
                format: Some(Format::Csv),
            }))
        );
    }

    #[test]
    fn rejects_multiple_inputs() {
        let error = parse_args(vec!["first.csv".into(), "second.csv".into()]).unwrap_err();
        assert!(error.contains("unexpected second input 'second.csv'"));
    }

    #[test]
    fn rejects_unknown_formats() {
        let error = parse_args(vec!["-".into(), "--format".into(), "yaml".into()]).unwrap_err();
        assert!(error.contains("invalid format 'yaml'"));
    }
}
