//! Cedar access evaluator for Universal Agents.
//!
//! The Python access service (`access_control`) runs this crate's binary as a
//! long-lived `serve-access` process and exchanges one JSON request and one
//! JSON decision per line (see `access`). The evaluator itself is a library so
//! a future Rust runtime can call it without a subprocess.

pub mod access;
