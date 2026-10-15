use std::env;
use std::io::{self, BufRead, Write};
use std::path::PathBuf;
use ua_cedar_authorizer::access::{AccessEvaluator, decide_line};

fn main() {
    if let Err(error) = run() {
        eprintln!("ua-cedar-authorizer: {error}");
        std::process::exit(2);
    }
}

fn run() -> Result<(), String> {
    let args = parse_args()?;
    if !matches!(args.command.as_str(), "validate-access" | "serve-access") {
        return Err(format!(
            "unsupported command {:?}\n{}",
            args.command,
            usage()
        ));
    }
    let evaluator = AccessEvaluator::load(&args.policy_path, &args.schema_path)?;
    if args.command == "validate-access" {
        // Load and strictly validate the policy set, print the ready line, exit.
        return write_json(&evaluator.ready());
    }
    serve_access(evaluator)
}

struct CliArgs {
    command: String,
    policy_path: PathBuf,
    schema_path: PathBuf,
}

fn parse_args() -> Result<CliArgs, String> {
    let mut args = env::args().skip(1);
    let command = args.next().ok_or_else(usage)?;
    let mut policy_path = None;
    let mut schema_path = None;
    while let Some(arg) = args.next() {
        let value = args.next().ok_or_else(usage)?;
        match arg.as_str() {
            "--policy" => policy_path = Some(PathBuf::from(value)),
            "--schema" => schema_path = Some(PathBuf::from(value)),
            _ => return Err(format!("unknown argument {arg:?}\n{}", usage())),
        }
    }
    Ok(CliArgs {
        command,
        policy_path: policy_path.ok_or_else(usage)?,
        schema_path: schema_path.ok_or_else(usage)?,
    })
}

fn usage() -> String {
    "usage: ua-cedar-authorizer <serve-access|validate-access> --policy <path> --schema <path>"
        .to_owned()
}

/// One JSON request per line on stdin, one decision per line on stdout.
fn serve_access(evaluator: AccessEvaluator) -> Result<(), String> {
    write_json(&evaluator.ready())?;
    let stdin = io::stdin();
    for line in stdin.lock().lines() {
        let line = line.map_err(|error| format!("stdin read failed: {error}"))?;
        if line.trim().is_empty() {
            continue;
        }
        write_json(&decide_line(&evaluator, &line))?;
    }
    Ok(())
}

fn write_json(value: &impl serde::Serialize) -> Result<(), String> {
    let stdout = io::stdout();
    let mut handle = stdout.lock();
    serde_json::to_writer(&mut handle, value)
        .map_err(|error| format!("stdout serialization failed: {error}"))?;
    handle
        .write_all(b"\n")
        .map_err(|error| format!("stdout write failed: {error}"))?;
    handle
        .flush()
        .map_err(|error| format!("stdout flush failed: {error}"))
}
