//! Loaders for the shipped ETF fixtures, shared by the test modules.
//!
//! Tests read the files the running service reads, from the repository, so an
//! edit to `data/` is exercised by the same run that verifies the engine.

use std::fs;
use std::path::PathBuf;

use serde::Deserialize;

use crate::domain::SeedEtf;
use crate::rules::{InvestorProfile, RulesSpec};

pub fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("crate lives one level below the repository root")
        .to_path_buf()
}

fn read(name: &str) -> String {
    let path = repo_root().join("data").join(name);
    fs::read_to_string(&path).unwrap_or_else(|error| panic!("read {name}: {error}"))
}

fn load<T: for<'de> Deserialize<'de>>(name: &str) -> T {
    serde_json::from_str(&read(name)).unwrap_or_else(|error| panic!("parse {name}: {error}"))
}

/// The rules specification, through the same validating parser the server uses.
pub fn load_rules() -> RulesSpec {
    RulesSpec::parse(&read("rules_spec.json")).unwrap_or_else(|error| panic!("{error}"))
}

pub fn load_profile() -> InvestorProfile {
    load("investor_profile.json")
}

pub fn load_etfs() -> Vec<SeedEtf> {
    load("etfs.json")
}

pub fn load_test_cases() -> Vec<EtfTestCase> {
    load("test_cases.json")
}

/// One labelled expectation from `data/test_cases.json`.
///
/// The decision is asserted exactly; the score is asserted as a reasoned range.
/// Pinning a single expected integer here would make the labels a transcript of
/// the implementation. The exact per-ETF scores are pinned separately, in the
/// regenerated baseline artifact, where a change shows up as a reviewable diff.
#[derive(Debug, Deserialize)]
pub struct EtfTestCase {
    pub case_id: String,
    pub etf_id: String,
    pub expected_decision: String,
    pub expected_score_range: [i32; 2],
    #[serde(default)]
    pub expected_hard_constraints: Vec<String>,
    #[serde(default)]
    pub expected_caps: Vec<String>,
    #[serde(default)]
    pub expected_missing_data: Vec<String>,
    #[serde(default)]
    pub expected_rule_codes: Vec<String>,
    pub rationale: String,
}
