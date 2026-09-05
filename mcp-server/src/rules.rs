//! The deterministic ETF evaluation engine and the decision vocabulary it works in.
//!
//! Deliberately free of database, network and server state. The tests in this
//! module therefore exercise the code that actually runs in production rather
//! than a reimplementation that can agree with it by coincidence.
//!
//! Two things live here and nowhere else:
//!
//! * the *interpretation* of `data/rules_spec.json` — bands, matrices, caps and
//!   thresholds are all data, so changing policy is a reviewable edit to a JSON
//!   file rather than a code change;
//! * the ordering `reject < research < shortlist`, which the override policy and
//!   every mutation path depend on.
//!
//! What the engine computes is **quality and fit against a configured investor
//! profile for a dated data snapshot**. It is not a return forecast, and nothing
//! in this module looks at price history.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// The only decisions this system recognises, ordered least to most attractive.
/// [`decision_rank`] depends on that ordering.
pub const DECISIONS: [&str; 3] = ["reject", "research", "shortlist"];

/// Every ETF field the specification is allowed to score on. Validated at load
/// so a typo in `rules_spec.json` fails at boot rather than silently removing a
/// component's weight from every evaluation.
pub const SCORABLE_FIELDS: [&str; 11] = [
    "asset_class",
    "region",
    "ucits",
    "distribution_policy",
    "replication",
    "ter",
    "aum_usd",
    "fund_age_years",
    "holdings_count",
    "top_10_concentration",
    "tracking_difference_3y",
];

// ---------------------------------------------------------------------------
// Specification
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RulesSpec {
    pub version: String,
    pub description: String,
    pub decision_vocabulary: Vec<String>,
    pub decision_order: Vec<String>,
    pub decision_thresholds: Vec<DecisionThreshold>,
    pub profile_fit_components: Vec<String>,
    pub region_classes: BTreeMap<String, String>,
    pub score_components: Vec<ScoreComponent>,
    pub preference_realisation: BTreeMap<String, String>,
    pub hard_constraints: Vec<HardConstraint>,
    pub missing_data_policy: MissingDataPolicy,
    pub decision_caps: Vec<DecisionCap>,
    /// Prose, handed to clients verbatim. The engine does not branch on it; the
    /// behaviour it describes is implemented by [`decision_rank`] and the
    /// mutation paths.
    pub override_policy: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DecisionThreshold {
    pub decision: String,
    pub min_score: i32,
    pub max_score: i32,
    pub note: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScoreComponent {
    pub key: String,
    pub weight: i32,
    pub description: String,
    pub metrics: Vec<Metric>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Direction {
    LowerIsBetter,
    HigherIsBetter,
}

/// One scored input. `threshold` is a *bound*, not a position in an array: see
/// [`Metric::score`] for why matching cannot depend on file order.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Band {
    pub code: String,
    /// Inclusive upper bound for `lower_is_better`, inclusive lower bound for
    /// `higher_is_better`. `null` marks the single fall-through band.
    pub threshold: Option<f64>,
    pub fraction: f64,
    pub note: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Category {
    pub fraction: f64,
    pub note: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Metric {
    /// A numeric metric scored into bands.
    Numeric {
        key: String,
        field: String,
        weight: i32,
        direction: Direction,
        #[serde(default)]
        absolute: bool,
        bands: Vec<Band>,
    },
    /// A text metric scored from an explicit vocabulary.
    Categorical {
        key: String,
        field: String,
        weight: i32,
        code_prefix: String,
        categories: BTreeMap<String, Category>,
    },
    /// A text metric scored against a matrix row chosen by an investor-profile
    /// value, so the same ETF scores differently for a different profile.
    ProfileMatrix {
        key: String,
        field: String,
        profile_key: String,
        weight: i32,
        code_prefix: String,
        #[serde(default)]
        map_through: Option<String>,
        matrix: BTreeMap<String, BTreeMap<String, f64>>,
    },
    /// A stated investor preference. Contributes nothing in either direction
    /// when the preference is switched off, rather than penalising every fund.
    Preference {
        key: String,
        preference: String,
        field: String,
        weight: i32,
        code_prefix: String,
        #[serde(default)]
        map_through: Option<String>,
        satisfied_when: Vec<String>,
        note: String,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HardConstraint {
    pub code: String,
    pub profile_key: String,
    pub field: String,
    pub required_value: Value,
    pub decision: String,
    pub bypassable: bool,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MissingDataPolicy {
    pub description: String,
    pub absent_metric_handling: String,
    pub critical_fields: Vec<String>,
    pub critical_fields_note: String,
    pub completeness_fields: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DecisionCap {
    pub code: String,
    pub max_decision: String,
    pub condition: CapCondition,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum CapCondition {
    CriticalFieldMissing,
    DataCompletenessBelow { value: f64 },
    ProfileFitBelow { value: f64 },
}

// ---------------------------------------------------------------------------
// Investor profile
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InvestorProfile {
    pub profile_id: String,
    pub version: String,
    pub description: String,
    pub base_currency: String,
    pub investment_horizon_years: i32,
    pub strategy: String,
    pub risk_tolerance: String,
    pub hard_constraints: BTreeMap<String, Value>,
    pub preferences: BTreeMap<String, bool>,
}

impl InvestorProfile {
    fn value_for(&self, key: &str) -> Option<String> {
        match key {
            "risk_tolerance" => Some(self.risk_tolerance.to_ascii_lowercase()),
            "strategy" => Some(self.strategy.to_ascii_lowercase()),
            "base_currency" => Some(self.base_currency.to_ascii_uppercase()),
            _ => None,
        }
    }

    fn prefers(&self, preference: &str) -> bool {
        self.preferences.get(preference).copied().unwrap_or(false)
    }

    fn constraint_enabled(&self, key: &str) -> bool {
        self.hard_constraints.get(key) == Some(&Value::Bool(true))
    }
}

// ---------------------------------------------------------------------------
// The scored view of one ETF
// ---------------------------------------------------------------------------

/// Exactly the ETF fields the engine scores on.
///
/// A dedicated type rather than the database row: the engine stays independent
/// of persistence, and adding a stored column cannot silently become policy.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct EtfFacts {
    pub etf_id: String,
    pub asset_class: String,
    pub region: String,
    pub ucits: bool,
    pub distribution_policy: String,
    pub replication: String,
    pub ter: Option<f64>,
    pub aum_usd: Option<i64>,
    pub fund_age_years: Option<f64>,
    pub holdings_count: Option<i32>,
    pub top_10_concentration: Option<f64>,
    pub tracking_difference_3y: Option<f64>,
}

enum FieldValue<'a> {
    Number(f64),
    Text(&'a str),
    Bool(bool),
    Absent,
}

impl EtfFacts {
    fn field(&self, name: &str) -> FieldValue<'_> {
        fn text(value: &str) -> FieldValue<'_> {
            if value.trim().is_empty() {
                FieldValue::Absent
            } else {
                FieldValue::Text(value)
            }
        }
        match name {
            "asset_class" => text(&self.asset_class),
            "region" => text(&self.region),
            "distribution_policy" => text(&self.distribution_policy),
            "replication" => text(&self.replication),
            "ucits" => FieldValue::Bool(self.ucits),
            "ter" => self.ter.map_or(FieldValue::Absent, FieldValue::Number),
            "aum_usd" => self
                .aum_usd
                .map_or(FieldValue::Absent, |value| FieldValue::Number(value as f64)),
            "fund_age_years" => self.fund_age_years.map_or(FieldValue::Absent, FieldValue::Number),
            "holdings_count" => self
                .holdings_count
                .map_or(FieldValue::Absent, |value| FieldValue::Number(value as f64)),
            "top_10_concentration" => {
                self.top_10_concentration.map_or(FieldValue::Absent, FieldValue::Number)
            }
            "tracking_difference_3y" => {
                self.tracking_difference_3y.map_or(FieldValue::Absent, FieldValue::Number)
            }
            // Unreachable for a validated specification; treated as absent so an
            // unvalidated one degrades to "unscored and reported" rather than to
            // a panic in a request path.
            _ => FieldValue::Absent,
        }
    }

    /// Whether a field carries a value at all. Used for data completeness, which
    /// is about the record rather than about any one component.
    pub fn has_value(&self, name: &str) -> bool {
        !matches!(self.field(name), FieldValue::Absent)
    }
}

// ---------------------------------------------------------------------------
// Evaluation output
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize)]
pub struct MatchedRule {
    pub component: String,
    pub metric: String,
    pub code: String,
    pub field: String,
    pub observed: Value,
    pub fraction: f64,
    pub weight: i32,
    pub points: f64,
    pub note: String,
}

/// One component's arithmetic, with the renormalisation made explicit.
///
/// Four numbers rather than two, because collapsing them is actively confusing.
/// A component with a nominal weight of 15 whose metrics were all available can
/// still *contribute* 16 points to the published score, once the missing weight
/// elsewhere is renormalised away — and reported as a bare "16 out of 15" that
/// reads as a bug. Naming the stages separates "what this component was worth",
/// "how much of it this record could answer", "what it earned out of that", and
/// "what that became after the score was rescaled to 100".
#[derive(Debug, Clone, Serialize)]
pub struct ComponentBreakdown {
    pub key: String,
    pub description: String,
    /// What the specification assigns this component out of 100.
    pub nominal_weight: i32,
    /// How much of that weight this ETF's record could actually be scored on.
    /// Zero means the component is unavailable, not that it scored badly.
    pub available_weight: i32,
    /// Points earned out of `available_weight`, before renormalisation.
    pub raw_earned_points: f64,
    /// This component's share of the published 0-100 score, after the available
    /// weight was renormalised. These sum exactly to `investment_score`.
    pub normalized_contribution: i32,
    /// True when no metric in this component could be scored for this ETF.
    pub unavailable: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct MissingDatum {
    pub field: String,
    pub component: String,
    pub metric: String,
    pub weight_removed: i32,
    pub reason: String,
    pub critical: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct NotApplicable {
    pub component: String,
    pub metric: String,
    pub weight_removed: i32,
    pub reason: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct HardConstraintHit {
    pub code: String,
    pub decision: String,
    pub bypassable: bool,
    pub message: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct AppliedCap {
    pub code: String,
    pub max_decision: String,
    pub message: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ProfileFit {
    pub components: Vec<String>,
    pub earned_points: f64,
    pub available_weight: i32,
    pub fraction: f64,
}

/// The renormalisation, published rather than implied.
///
/// Without it the component table cannot be reconciled with the score: a reader
/// sees contributions that exceed nominal weights and has no way to tell whether
/// that is policy or a defect.
#[derive(Debug, Clone, Serialize)]
pub struct Normalization {
    /// The nominal weights of every component, which always sum to 100.
    pub total_weight: i32,
    /// The weight this ETF's record could actually be scored on.
    pub available_weight: i32,
    /// Points earned out of `available_weight`.
    pub raw_earned_points: f64,
    /// `100 / available_weight`: what every raw point was multiplied by.
    pub factor: f64,
    pub note: &'static str,
}

const NORMALIZATION_NOTE: &str = "investment_score is raw_earned_points rescaled from \
available_weight to 100. Metrics with no value leave the denominator rather than scoring zero, so \
a component's normalized_contribution can exceed its nominal_weight when weight was withdrawn \
elsewhere. That is renormalisation, not a component earning more than it is worth.";

#[derive(Debug, Clone, Serialize)]
pub struct Evaluation {
    pub etf_id: String,
    pub investment_score: i32,
    pub decision: String,
    /// The decision the score alone implies, before hard constraints and caps.
    /// Published so a capped or rejected outcome is explainable rather than
    /// merely asserted.
    pub score_decision: String,
    /// Each component's normalised contribution. Sums exactly to
    /// `investment_score`; see [`Normalization`] for why a value here can exceed
    /// the component's nominal weight.
    pub components: BTreeMap<String, i32>,
    pub component_breakdown: Vec<ComponentBreakdown>,
    pub matched_rules: Vec<MatchedRule>,
    pub hard_constraints: Vec<HardConstraintHit>,
    pub missing_data: Vec<MissingDatum>,
    pub not_applicable: Vec<NotApplicable>,
    pub data_completeness: f64,
    pub profile_fit: ProfileFit,
    pub applied_caps: Vec<AppliedCap>,
    pub normalization: Normalization,
    pub rules_version: String,
    pub profile_version: String,
    pub explanation: String,
    pub note: &'static str,
}

/// Attached to every evaluation. The distinction it draws is the whole point of
/// the project, so it travels with the data rather than living only in a prompt.
pub const SCORE_MEANING: &str =
    "investment_score measures deterministic ETF quality and fit against the configured investor \
profile for the dated data snapshot in data/etfs.json. It is not an expected-return forecast, not \
financial advice, and not a trade instruction.";

// ---------------------------------------------------------------------------
// Specification validation
// ---------------------------------------------------------------------------

impl Metric {
    pub fn key(&self) -> &str {
        match self {
            Metric::Numeric { key, .. }
            | Metric::Categorical { key, .. }
            | Metric::ProfileMatrix { key, .. }
            | Metric::Preference { key, .. } => key,
        }
    }

    pub fn field(&self) -> &str {
        match self {
            Metric::Numeric { field, .. }
            | Metric::Categorical { field, .. }
            | Metric::ProfileMatrix { field, .. }
            | Metric::Preference { field, .. } => field,
        }
    }

    pub fn weight(&self) -> i32 {
        match self {
            Metric::Numeric { weight, .. }
            | Metric::Categorical { weight, .. }
            | Metric::ProfileMatrix { weight, .. }
            | Metric::Preference { weight, .. } => *weight,
        }
    }
}

impl RulesSpec {
    /// Parse and validate a specification.
    ///
    /// Both the server and the test loader go through here, so an inconsistent
    /// specification cannot reach an evaluation from either direction.
    pub fn parse(raw: &str) -> Result<Self, String> {
        let spec: RulesSpec =
            serde_json::from_str(raw).map_err(|error| format!("rules_spec.json is invalid: {error}"))?;
        spec.validate()?;
        Ok(spec)
    }

    pub fn validate(&self) -> Result<(), String> {
        if self.decision_order != DECISIONS {
            return Err(format!(
                "decision_order must be exactly {:?}; the ordering is load-bearing for the override policy",
                DECISIONS
            ));
        }
        let vocabulary: BTreeSet<&str> = self.decision_vocabulary.iter().map(String::as_str).collect();
        if vocabulary != DECISIONS.iter().copied().collect() {
            return Err(format!("decision_vocabulary must be exactly {DECISIONS:?}"));
        }

        // Thresholds must tile 0..=100 with no gap and no overlap, so every score
        // resolves to exactly one decision without depending on file order.
        let mut bands: Vec<&DecisionThreshold> = self.decision_thresholds.iter().collect();
        bands.sort_by_key(|band| band.min_score);
        if bands.len() != DECISIONS.len() {
            return Err("decision_thresholds must define exactly one band per decision".to_string());
        }
        let mut expected_min = 0;
        for band in &bands {
            if !DECISIONS.contains(&band.decision.as_str()) {
                return Err(format!("unknown decision {:?} in decision_thresholds", band.decision));
            }
            if band.min_score != expected_min {
                return Err(format!(
                    "decision_thresholds leave a gap or overlap at score {expected_min}"
                ));
            }
            if band.max_score < band.min_score {
                return Err(format!("decision band {:?} is inverted", band.decision));
            }
            expected_min = band.max_score + 1;
        }
        if expected_min != 101 {
            return Err("decision_thresholds must cover scores 0 through 100".to_string());
        }
        // The bands must ascend in the same order as the decision vocabulary, or
        // a higher score would mean a less attractive decision.
        for pair in bands.windows(2) {
            if decision_rank(&pair[0].decision) >= decision_rank(&pair[1].decision) {
                return Err("decision_thresholds must ascend from reject to shortlist".to_string());
            }
        }

        let mut total_weight = 0;
        let mut component_keys = BTreeSet::new();
        for component in &self.score_components {
            if !component_keys.insert(component.key.as_str()) {
                return Err(format!("duplicate score component {:?}", component.key));
            }
            total_weight += component.weight;
            let metric_weight: i32 = component.metrics.iter().map(Metric::weight).sum();
            if metric_weight != component.weight {
                return Err(format!(
                    "component {:?} declares weight {} but its metrics sum to {metric_weight}",
                    component.key, component.weight
                ));
            }
            let mut metric_keys = BTreeSet::new();
            for metric in &component.metrics {
                if !metric_keys.insert(metric.key()) {
                    return Err(format!(
                        "duplicate metric {:?} in component {:?}",
                        metric.key(),
                        component.key
                    ));
                }
                if metric.weight() < 0 {
                    return Err(format!("metric {:?} has a negative weight", metric.key()));
                }
                if !SCORABLE_FIELDS.contains(&metric.field()) {
                    return Err(format!(
                        "metric {:?} scores on unknown ETF field {:?}",
                        metric.key(),
                        metric.field()
                    ));
                }
                self.validate_metric(&component.key, metric)?;
            }
        }
        if total_weight != 100 {
            return Err(format!("score component weights must sum to 100, not {total_weight}"));
        }

        for key in &self.profile_fit_components {
            if !component_keys.contains(key.as_str()) {
                return Err(format!("profile_fit_components names unknown component {key:?}"));
            }
        }

        for constraint in &self.hard_constraints {
            if !DECISIONS.contains(&constraint.decision.as_str()) {
                return Err(format!(
                    "hard constraint {:?} names unknown decision {:?}",
                    constraint.code, constraint.decision
                ));
            }
            if !SCORABLE_FIELDS.contains(&constraint.field.as_str()) {
                return Err(format!(
                    "hard constraint {:?} reads unknown ETF field {:?}",
                    constraint.code, constraint.field
                ));
            }
        }

        for cap in &self.decision_caps {
            if !DECISIONS.contains(&cap.max_decision.as_str()) {
                return Err(format!(
                    "decision cap {:?} names unknown decision {:?}",
                    cap.code, cap.max_decision
                ));
            }
        }

        for field in self
            .missing_data_policy
            .critical_fields
            .iter()
            .chain(&self.missing_data_policy.completeness_fields)
        {
            if !SCORABLE_FIELDS.contains(&field.as_str()) {
                return Err(format!("missing_data_policy names unknown ETF field {field:?}"));
            }
        }

        for (region, class) in &self.region_classes {
            if region.trim().is_empty() || class.trim().is_empty() {
                return Err("region_classes contains an empty mapping".to_string());
            }
        }
        Ok(())
    }

    fn validate_metric(&self, component: &str, metric: &Metric) -> Result<(), String> {
        let context = format!("component {component:?} metric {:?}", metric.key());
        match metric {
            Metric::Numeric { bands, .. } => {
                if bands.is_empty() {
                    return Err(format!("{context} declares no bands"));
                }
                let open_ended = bands.iter().filter(|band| band.threshold.is_none()).count();
                if open_ended != 1 {
                    return Err(format!(
                        "{context} must have exactly one fall-through band with threshold null, not {open_ended}"
                    ));
                }
                let mut seen: Vec<f64> = Vec::new();
                let mut codes = BTreeSet::new();
                for band in bands {
                    if !codes.insert(band.code.as_str()) {
                        return Err(format!("{context} repeats band code {:?}", band.code));
                    }
                    if !(0.0..=1.0).contains(&band.fraction) {
                        return Err(format!(
                            "{context} band {:?} has fraction {} outside 0..=1",
                            band.code, band.fraction
                        ));
                    }
                    if let Some(threshold) = band.threshold {
                        if !threshold.is_finite() {
                            return Err(format!("{context} band {:?} threshold is not finite", band.code));
                        }
                        if seen.iter().any(|other| (other - threshold).abs() < f64::EPSILON) {
                            return Err(format!(
                                "{context} repeats threshold {threshold}; bands would be ambiguous"
                            ));
                        }
                        seen.push(threshold);
                    }
                }
                Ok(())
            }
            Metric::Categorical { categories, .. } => {
                if categories.is_empty() {
                    return Err(format!("{context} declares no categories"));
                }
                for (name, category) in categories {
                    if !(0.0..=1.0).contains(&category.fraction) {
                        return Err(format!("{context} category {name:?} is outside 0..=1"));
                    }
                }
                Ok(())
            }
            Metric::ProfileMatrix { matrix, map_through, profile_key, .. } => {
                if matrix.is_empty() {
                    return Err(format!("{context} declares an empty matrix"));
                }
                if !["risk_tolerance", "strategy", "base_currency"].contains(&profile_key.as_str()) {
                    return Err(format!("{context} keys on unknown profile field {profile_key:?}"));
                }
                for (row, columns) in matrix {
                    for (column, fraction) in columns {
                        if !(0.0..=1.0).contains(fraction) {
                            return Err(format!(
                                "{context} matrix cell [{row}][{column}] is outside 0..=1"
                            ));
                        }
                    }
                }
                self.validate_map_through(&context, map_through)
            }
            Metric::Preference { satisfied_when, map_through, .. } => {
                if satisfied_when.is_empty() {
                    return Err(format!("{context} satisfies on nothing"));
                }
                self.validate_map_through(&context, map_through)
            }
        }
    }

    fn validate_map_through(&self, context: &str, map_through: &Option<String>) -> Result<(), String> {
        match map_through.as_deref() {
            None | Some("region_classes") => Ok(()),
            Some(other) => Err(format!("{context} maps through unknown table {other:?}")),
        }
    }

    fn threshold_for(&self, score: i32) -> Option<&DecisionThreshold> {
        self.decision_thresholds
            .iter()
            .find(|band| score >= band.min_score && score <= band.max_score)
    }
}

// ---------------------------------------------------------------------------
// Decision vocabulary
// ---------------------------------------------------------------------------

/// Attractiveness ordering. Higher is a more attractive investment candidate;
/// unknown values rank below every real decision so they can never win a
/// comparison.
pub fn decision_rank(value: &str) -> i32 {
    match value {
        "reject" => 0,
        "research" => 1,
        "shortlist" => 2,
        _ => -1,
    }
}

/// Canonicalise a decision, or explain why it is not one.
///
/// Returns the rejection message rather than a transport error so this module
/// stays free of the MCP wire types; callers wrap it.
pub fn normalize_decision(value: &str) -> Result<String, String> {
    let value = value.trim().to_ascii_lowercase();
    if DECISIONS.contains(&value.as_str()) {
        Ok(value)
    } else {
        Err("decision must be reject, research, or shortlist".to_string())
    }
}

/// The less attractive of two decisions. Used to apply a policy cap.
fn cap_decision(decision: &str, cap: &str) -> String {
    if decision_rank(cap) < decision_rank(decision) {
        cap.to_string()
    } else {
        decision.to_string()
    }
}

// ---------------------------------------------------------------------------
// Scoring
// ---------------------------------------------------------------------------

struct ScoredMetric {
    code: String,
    fraction: f64,
    note: String,
    observed: Value,
}

enum MetricOutcome {
    Scored(ScoredMetric),
    /// The ETF record has no usable value for this metric.
    Missing { reason: String },
    /// The metric does not apply to this profile at all.
    NotApplicable { reason: String },
}

fn band_for(bands: &[Band], direction: Direction, value: f64) -> &Band {
    // Matching is by *bound*, never by position, so reordering the bands in the
    // JSON cannot change a decision. `rules_spec.json` happens to list them in
    // order, which is exactly the kind of coincidence that turns file layout
    // into unreviewed policy.
    let candidate = bands
        .iter()
        .filter_map(|band| band.threshold.map(|threshold| (band, threshold)))
        .filter(|(_, threshold)| match direction {
            Direction::LowerIsBetter => value <= *threshold,
            Direction::HigherIsBetter => value >= *threshold,
        })
        .min_by(|left, right| match direction {
            Direction::LowerIsBetter => left.1.total_cmp(&right.1),
            Direction::HigherIsBetter => right.1.total_cmp(&left.1),
        })
        .map(|(band, _)| band);
    candidate.unwrap_or_else(|| {
        bands
            .iter()
            .find(|band| band.threshold.is_none())
            .expect("a validated metric has exactly one fall-through band")
    })
}

fn map_value<'a>(spec: &'a RulesSpec, map_through: &Option<String>, value: &'a str) -> Option<String> {
    let lowered = value.to_ascii_lowercase();
    match map_through.as_deref() {
        Some("region_classes") => spec.region_classes.get(&lowered).cloned(),
        _ => Some(lowered),
    }
}

fn score_metric(
    spec: &RulesSpec,
    profile: &InvestorProfile,
    etf: &EtfFacts,
    metric: &Metric,
) -> MetricOutcome {
    match metric {
        Metric::Numeric { field, direction, absolute, bands, .. } => {
            let FieldValue::Number(raw) = etf.field(field) else {
                return MetricOutcome::Missing { reason: "no value in the ETF record".to_string() };
            };
            if !raw.is_finite() {
                return MetricOutcome::Missing { reason: "value is not a finite number".to_string() };
            }
            let value = if *absolute { raw.abs() } else { raw };
            let band = band_for(bands, *direction, value);
            MetricOutcome::Scored(ScoredMetric {
                code: band.code.clone(),
                fraction: band.fraction,
                note: band.note.clone(),
                observed: serde_json::json!(raw),
            })
        }
        Metric::Categorical { field, code_prefix, categories, .. } => {
            let FieldValue::Text(raw) = etf.field(field) else {
                return MetricOutcome::Missing { reason: "no value in the ETF record".to_string() };
            };
            let lowered = raw.to_ascii_lowercase();
            match categories.get(&lowered) {
                Some(category) => MetricOutcome::Scored(ScoredMetric {
                    code: format!("{code_prefix}-{}", lowered.to_ascii_uppercase()),
                    fraction: category.fraction,
                    note: category.note.clone(),
                    observed: serde_json::json!(raw),
                }),
                // An unrecognised value is not the same thing as a missing one,
                // and it is certainly not evidence of zero quality. It is
                // unscored and reported, so the record gets fixed rather than
                // the fund silently penalised.
                None => MetricOutcome::Missing {
                    reason: format!("value {raw:?} is not in the specification's vocabulary"),
                },
            }
        }
        Metric::ProfileMatrix { field, profile_key, code_prefix, map_through, matrix, .. } => {
            let Some(row_key) = profile.value_for(profile_key) else {
                return MetricOutcome::Missing {
                    reason: format!("investor profile has no {profile_key}"),
                };
            };
            let Some(row) = matrix.get(&row_key) else {
                return MetricOutcome::Missing {
                    reason: format!("no matrix row for {profile_key}={row_key:?}"),
                };
            };
            let FieldValue::Text(raw) = etf.field(field) else {
                return MetricOutcome::Missing { reason: "no value in the ETF record".to_string() };
            };
            let Some(column) = map_value(spec, map_through, raw) else {
                return MetricOutcome::Missing {
                    reason: format!("value {raw:?} has no entry in region_classes"),
                };
            };
            match row.get(&column) {
                Some(fraction) => MetricOutcome::Scored(ScoredMetric {
                    code: format!(
                        "{code_prefix}-{}-{}",
                        row_key.to_ascii_uppercase(),
                        column.to_ascii_uppercase()
                    ),
                    fraction: *fraction,
                    note: format!(
                        "{profile_key}={row_key} against {field}={raw} (class {column})"
                    ),
                    observed: serde_json::json!(raw),
                }),
                None => MetricOutcome::Missing {
                    reason: format!("matrix row {row_key:?} has no column {column:?}"),
                },
            }
        }
        Metric::Preference {
            preference,
            field,
            code_prefix,
            map_through,
            satisfied_when,
            note,
            ..
        } => {
            if !profile.prefers(preference) {
                return MetricOutcome::NotApplicable {
                    reason: format!("the investor profile does not express the {preference} preference"),
                };
            }
            let FieldValue::Text(raw) = etf.field(field) else {
                return MetricOutcome::Missing { reason: "no value in the ETF record".to_string() };
            };
            let Some(compared) = map_value(spec, map_through, raw) else {
                return MetricOutcome::Missing {
                    reason: format!("value {raw:?} has no entry in region_classes"),
                };
            };
            let satisfied = satisfied_when
                .iter()
                .any(|candidate| candidate.eq_ignore_ascii_case(&compared));
            MetricOutcome::Scored(ScoredMetric {
                code: format!("{code_prefix}-{}", if satisfied { "MET" } else { "UNMET" }),
                fraction: if satisfied { 1.0 } else { 0.0 },
                note: format!(
                    "{note} Observed {field}={raw}{}.",
                    if compared == raw.to_ascii_lowercase() {
                        String::new()
                    } else {
                        format!(" (class {compared})")
                    }
                ),
                observed: serde_json::json!(raw),
            })
        }
    }
}

/// Distribute `target` integer points across weighted parts so the reported
/// components sum exactly to the published score.
///
/// Largest-remainder, with ties broken by specification order, so the output is
/// a pure function of the inputs.
fn distribute(parts: &[(String, f64)], target: i32) -> BTreeMap<String, i32> {
    let mut floors: Vec<(usize, String, i32, f64)> = parts
        .iter()
        .enumerate()
        .map(|(index, (key, value))| {
            let floor = value.floor();
            (index, key.clone(), floor as i32, value - floor)
        })
        .collect();
    let assigned: i32 = floors.iter().map(|entry| entry.2).sum();
    let mut remaining = (target - assigned).max(0) as usize;

    let mut order: Vec<usize> = (0..floors.len()).collect();
    order.sort_by(|left, right| {
        floors[*right]
            .3
            .total_cmp(&floors[*left].3)
            .then_with(|| floors[*left].0.cmp(&floors[*right].0))
    });
    for index in order {
        if remaining == 0 {
            break;
        }
        floors[index].2 += 1;
        remaining -= 1;
    }
    floors.into_iter().map(|(_, key, points, _)| (key, points)).collect()
}

fn round_half_up(value: f64) -> i32 {
    (value + 0.5).floor() as i32
}

/// Evaluate one ETF against the specification and the investor profile.
///
/// Pure: the same three inputs always produce the same evaluation, which is what
/// makes it safe to recompute at read time, again at write time, and once more
/// in a test that publishes the result.
pub fn evaluate(spec: &RulesSpec, profile: &InvestorProfile, etf: &EtfFacts) -> Evaluation {
    let mut matched_rules = Vec::new();
    let mut missing_data = Vec::new();
    let mut not_applicable = Vec::new();
    let mut breakdown = Vec::new();
    let mut earned_parts: Vec<(String, f64)> = Vec::new();
    let mut total_earned = 0.0;
    let mut total_scored_weight = 0;
    let critical: BTreeSet<&str> = spec
        .missing_data_policy
        .critical_fields
        .iter()
        .map(String::as_str)
        .collect();

    for component in &spec.score_components {
        let mut component_earned = 0.0;
        let mut component_weight = 0;
        for metric in &component.metrics {
            match score_metric(spec, profile, etf, metric) {
                MetricOutcome::Scored(scored) => {
                    let points = scored.fraction * f64::from(metric.weight());
                    component_earned += points;
                    component_weight += metric.weight();
                    matched_rules.push(MatchedRule {
                        component: component.key.clone(),
                        metric: metric.key().to_string(),
                        code: scored.code,
                        field: metric.field().to_string(),
                        observed: scored.observed,
                        fraction: scored.fraction,
                        weight: metric.weight(),
                        points,
                        note: scored.note,
                    });
                }
                MetricOutcome::Missing { reason } => missing_data.push(MissingDatum {
                    field: metric.field().to_string(),
                    component: component.key.clone(),
                    metric: metric.key().to_string(),
                    weight_removed: metric.weight(),
                    reason,
                    critical: critical.contains(metric.field()),
                }),
                MetricOutcome::NotApplicable { reason } => not_applicable.push(NotApplicable {
                    component: component.key.clone(),
                    metric: metric.key().to_string(),
                    weight_removed: metric.weight(),
                    reason,
                }),
            }
        }
        total_earned += component_earned;
        total_scored_weight += component_weight;
        earned_parts.push((component.key.clone(), component_earned));
        breakdown.push(ComponentBreakdown {
            key: component.key.clone(),
            description: component.description.clone(),
            nominal_weight: component.weight,
            available_weight: component_weight,
            raw_earned_points: (component_earned * 100.0).round() / 100.0,
            normalized_contribution: 0,
            unavailable: component_weight == 0,
        });
    }

    // Absent weight leaves the denominator rather than counting as zero quality.
    let investment_score = if total_scored_weight > 0 {
        round_half_up(100.0 * total_earned / f64::from(total_scored_weight)).clamp(0, 100)
    } else {
        0
    };
    let factor = if total_scored_weight > 0 {
        100.0 / f64::from(total_scored_weight)
    } else {
        0.0
    };
    let scaled: Vec<(String, f64)> = earned_parts
        .iter()
        .map(|(key, points)| (key.clone(), points * factor))
        .collect();
    let components = distribute(&scaled, investment_score);
    for entry in &mut breakdown {
        entry.normalized_contribution = components.get(&entry.key).copied().unwrap_or(0);
    }

    // Completeness describes the record, not any single component, so it is
    // measured over the declared field list rather than over scored weight.
    let completeness_fields = &spec.missing_data_policy.completeness_fields;
    let unusable: BTreeSet<&str> = missing_data.iter().map(|entry| entry.field.as_str()).collect();
    let present = completeness_fields
        .iter()
        .filter(|field| etf.has_value(field) && !unusable.contains(field.as_str()))
        .count();
    let data_completeness = if completeness_fields.is_empty() {
        1.0
    } else {
        (present as f64 / completeness_fields.len() as f64 * 10_000.0).round() / 10_000.0
    };

    let mut fit_earned = 0.0;
    let mut fit_weight = 0;
    for key in &spec.profile_fit_components {
        if let Some(entry) = breakdown.iter().find(|entry| &entry.key == key) {
            fit_earned += entry.raw_earned_points;
            fit_weight += entry.available_weight;
        }
    }
    let profile_fit = ProfileFit {
        components: spec.profile_fit_components.clone(),
        earned_points: (fit_earned * 100.0).round() / 100.0,
        available_weight: fit_weight,
        fraction: if fit_weight > 0 {
            (fit_earned / f64::from(fit_weight) * 10_000.0).round() / 10_000.0
        } else {
            0.0
        },
    };

    let hard_constraints = hard_constraint_hits(spec, profile, etf);

    let score_decision = spec
        .threshold_for(investment_score)
        .map(|band| band.decision.clone())
        // A validated specification tiles 0..=100, so this is unreachable; the
        // conservative fallback is the least attractive decision, never the most.
        .unwrap_or_else(|| "reject".to_string());

    let mut applied_caps = Vec::new();
    let mut decision = score_decision.clone();
    for cap in &spec.decision_caps {
        let triggered = match &cap.condition {
            CapCondition::CriticalFieldMissing => missing_data.iter().any(|entry| entry.critical),
            CapCondition::DataCompletenessBelow { value } => data_completeness < *value,
            CapCondition::ProfileFitBelow { value } => profile_fit.fraction < *value,
        };
        if triggered {
            decision = cap_decision(&decision, &cap.max_decision);
            applied_caps.push(AppliedCap {
                code: cap.code.clone(),
                max_decision: cap.max_decision.clone(),
                message: cap.message.clone(),
            });
        }
    }

    // A hard constraint is not a cap. It replaces the decision outright, and no
    // score can lift it.
    if let Some(hit) = hard_constraints.first() {
        decision = hit.decision.clone();
    }

    let explanation = explain(
        investment_score,
        &score_decision,
        &decision,
        &hard_constraints,
        &applied_caps,
        &missing_data,
        &breakdown,
        total_scored_weight,
    );

    Evaluation {
        etf_id: etf.etf_id.clone(),
        investment_score,
        decision,
        score_decision,
        components,
        component_breakdown: breakdown,
        matched_rules,
        hard_constraints,
        missing_data,
        not_applicable,
        data_completeness,
        profile_fit,
        applied_caps,
        normalization: Normalization {
            total_weight: spec.score_components.iter().map(|c| c.weight).sum(),
            available_weight: total_scored_weight,
            raw_earned_points: (total_earned * 100.0).round() / 100.0,
            factor: (factor * 10_000.0).round() / 10_000.0,
            note: NORMALIZATION_NOTE,
        },
        rules_version: spec.version.clone(),
        profile_version: profile.version.clone(),
        explanation,
        note: SCORE_MEANING,
    }
}

#[allow(clippy::too_many_arguments)]
fn explain(
    score: i32,
    score_decision: &str,
    decision: &str,
    hard_constraints: &[HardConstraintHit],
    caps: &[AppliedCap],
    missing: &[MissingDatum],
    breakdown: &[ComponentBreakdown],
    available_weight: i32,
) -> String {
    let mut lines = vec![
        format!("Investment score: {score}/100 (quality and profile fit, not a return forecast)"),
        format!("Score band: {score_decision}"),
    ];
    let total_weight: i32 = breakdown.iter().map(|entry| entry.nominal_weight).sum();
    if available_weight < total_weight {
        lines.push(format!(
            "Scored on {available_weight} of {total_weight} weight; the rest had no data and was \
renormalised away rather than scored as zero"
        ));
    }
    // Named rather than merely implied by a zero: a component with no data at all
    // is a different statement from a component that scored badly, and the two are
    // indistinguishable in the contribution column.
    let unavailable: Vec<&str> = breakdown
        .iter()
        .filter(|entry| entry.unavailable)
        .map(|entry| entry.key.as_str())
        .collect();
    if !unavailable.is_empty() {
        lines.push(format!(
            "Unavailable components (no data, not scored zero): {}",
            unavailable.join(", ")
        ));
    }
    for hit in hard_constraints {
        lines.push(format!(
            "Hard constraint {}: {} ({})",
            hit.code,
            hit.message,
            if hit.bypassable { "bypassable with a human override" } else { "not bypassable" }
        ));
    }
    let critical: Vec<&str> = missing
        .iter()
        .filter(|entry| entry.critical)
        .map(|entry| entry.field.as_str())
        .collect();
    if !critical.is_empty() {
        lines.push(format!("Missing critical fields: {}", critical.join(", ")));
    }
    for cap in caps {
        lines.push(format!("Policy cap {}: at most {}", cap.code, cap.max_decision));
    }
    lines.push(format!("Effective decision: {decision}"));
    lines.join("\n")
}

/// Which hard constraints this ETF violates under this profile.
///
/// Enabled entirely by the profile: a constraint whose `profile_key` is not
/// switched on is not evaluated at all, so policy lives in
/// `investor_profile.json` rather than in this file.
pub fn hard_constraint_hits(
    spec: &RulesSpec,
    profile: &InvestorProfile,
    etf: &EtfFacts,
) -> Vec<HardConstraintHit> {
    spec.hard_constraints
        .iter()
        .filter(|constraint| profile.constraint_enabled(&constraint.profile_key))
        .filter(|constraint| !field_matches(etf, &constraint.field, &constraint.required_value))
        .map(|constraint| HardConstraintHit {
            code: constraint.code.clone(),
            decision: constraint.decision.clone(),
            bypassable: constraint.bypassable,
            message: constraint.message.clone(),
        })
        .collect()
}

fn field_matches(etf: &EtfFacts, field: &str, required: &Value) -> bool {
    match (etf.field(field), required) {
        (FieldValue::Bool(actual), Value::Bool(expected)) => actual == *expected,
        (FieldValue::Text(actual), Value::String(expected)) => actual.eq_ignore_ascii_case(expected),
        (FieldValue::Number(actual), Value::Number(expected)) => {
            expected.as_f64().is_some_and(|expected| (actual - expected).abs() < f64::EPSILON)
        }
        // A required value the record cannot answer is not a match. Failing open
        // here would let an incomplete record satisfy a hard constraint.
        _ => false,
    }
}

/// Whether a hard constraint forbids this decision for this ETF, and which one.
///
/// Every mutation path calls this immediately before the write, so the check is
/// applied identically wherever state changes and cannot be restated
/// inconsistently per call site.
///
/// `override_applied` records that an authenticated human has supplied a
/// rationale. It can only relax a constraint marked `bypassable`; a
/// non-bypassable constraint is refused whatever the actor claims.
pub fn blocking_hard_constraint(
    spec: &RulesSpec,
    profile: &InvestorProfile,
    etf: &EtfFacts,
    decision: &str,
    override_applied: bool,
) -> Option<HardConstraintHit> {
    hard_constraint_hits(spec, profile, etf).into_iter().find(|hit| {
        decision_rank(decision) > decision_rank(&hit.decision)
            && !(hit.bypassable && override_applied)
    })
}

/// The relationship between a model recommendation and the deterministic
/// decision, and whether default policy permits it.
///
/// `reject < research < shortlist` measures increasing attractiveness, so the
/// permitted direction for a model is *downwards*: it may counsel caution, never
/// optimism.
///
/// Being permitted is not the same as being adopted. `default_decision` is the
/// deterministic decision in every case, including when the recommendation is
/// allowed, because an advisory opinion that silently became the default would
/// make the model the decision authority in the conservative direction.
pub fn compare_recommendation(deterministic: &str, recommendation: &str) -> RecommendationComparison {
    let deterministic_rank = decision_rank(deterministic);
    let recommendation_rank = decision_rank(recommendation);
    let relationship = if recommendation_rank < deterministic_rank {
        "more_conservative"
    } else if recommendation_rank == deterministic_rank {
        "equal"
    } else {
        "more_optimistic"
    };
    RecommendationComparison {
        relationship,
        allowed: recommendation_rank <= deterministic_rank,
        policy_violation: (recommendation_rank > deterministic_rank).then_some(
            "A model recommendation may never be more optimistic than the deterministic decision. \
An explicit human override is represented separately.",
        ),
        deterministic_decision: deterministic.to_string(),
        default_decision: deterministic.to_string(),
        note: ADVISORY_NOTE,
    }
}

/// Attached to every recommendation comparison, because "allowed" reads as
/// "adopted" unless the difference is stated.
pub const ADVISORY_NOTE: &str = "A model recommendation is advisory in both directions. Whether it \
is allowed or refused, the decision that stands by default is the deterministic one, and only an \
authenticated human may move away from it — upward or downward — with a recorded rationale.";

#[derive(Debug, Clone, Serialize)]
pub struct RecommendationComparison {
    pub relationship: &'static str,
    pub allowed: bool,
    pub policy_violation: Option<&'static str>,
    /// The engine's decision, restated so a client comparing the two never has to
    /// carry it separately.
    pub deterministic_decision: String,
    /// What the system decides absent a human override. Always the deterministic
    /// decision: a model recommendation is advisory in *both* directions, so a
    /// conservative one does not silently redefine the default either.
    pub default_decision: String,
    pub note: &'static str,
}

// ---------------------------------------------------------------------------
// Decision authority
// ---------------------------------------------------------------------------

/// What an approved decision resolves to, and who is responsible for it.
///
/// One vocabulary, used by every mutation path and by the approval UI:
///
/// * `rules_decision` — the deterministic engine's result. **This is the
///   authority, and it is the default presented to a human.**
/// * `llm_recommendation` — advisory only. It may equal the deterministic
///   decision or be more conservative; it may never be more optimistic, and it
///   never becomes the default.
/// * `requested_decision` — what the authenticated human actually chose.
///
/// An earlier revision derived the default as
/// `llm_recommendation.unwrap_or(rules_decision)`, which handed the model the
/// default whenever it was more conservative: with the engine at `shortlist` and
/// the model at `research`, a person choosing `shortlist` — the deterministic
/// result — was recorded as *overriding the system*. That inverts the whole
/// claim. A model that cannot promote must not be able to demote either.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DecisionAuthority {
    /// Authoritative, from the engine.
    pub rules_decision: String,
    /// Advisory, from the model.
    pub llm_recommendation: Option<String>,
    /// What the human is shown as the default. Always `rules_decision`.
    pub default_decision: String,
    /// What the human chose.
    pub requested_decision: String,
    /// Whether the human moved away from the deterministic decision.
    pub override_applied: bool,
    /// The chosen decision when, and only when, it is an override.
    pub human_override_decision: Option<String>,
}

/// Reconcile an approved decision against the deterministic authority.
///
/// Pure, so every case in the trust matrix is unit-testable without a database,
/// a token or a server. The mutation path calls this immediately after locking
/// the row and recomputing the engine, and refuses on any `Err`.
pub fn reconcile_decision(
    rules_decision: &str,
    llm_recommendation: Option<&str>,
    requested_decision: &str,
    override_requested: bool,
) -> Result<DecisionAuthority, String> {
    let rules_decision = normalize_decision(rules_decision)?;
    let requested_decision = normalize_decision(requested_decision)?;
    let llm_recommendation = llm_recommendation.map(normalize_decision).transpose()?;

    // The advisory ceiling, re-enforced below the model on every path.
    if let Some(recommendation) = llm_recommendation.as_deref()
        && decision_rank(recommendation) > decision_rank(&rules_decision)
    {
        return Err(
            "Policy violation: a model recommendation may never be more optimistic than the \
deterministic decision; an explicit human override is represented separately"
                .to_string(),
        );
    }

    // The default is the engine's decision, whatever the model said.
    let override_applied = requested_decision != rules_decision;
    if override_applied != override_requested {
        return Err(format!(
            "Approval token override flag does not match the approved decision. The deterministic \
engine returned '{rules_decision}' and the approved decision is '{requested_decision}', so this \
{} an explicit human override.",
            if override_applied { "requires" } else { "is not" }
        ));
    }

    Ok(DecisionAuthority {
        default_decision: rules_decision.clone(),
        human_override_decision: override_applied.then(|| requested_decision.clone()),
        rules_decision,
        llm_recommendation,
        requested_decision,
        override_applied,
    })
}

#[cfg(test)]
mod tests {
    //! Engine behaviour, asserted against the shipped fixtures through the
    //! shipped Rust implementation.
    //!
    //! There is deliberately no second implementation of the scoring policy
    //! anywhere in this repository. A published accuracy figure that comes from
    //! a reimplementation describes code nobody deployed.

    use std::fs;

    use serde_json::json;
    use sha2::{Digest, Sha256};

    use super::*;
    use crate::fixtures::{load_etfs, load_profile, load_rules, load_test_cases, repo_root};

    fn decide(spec: &RulesSpec, profile: &InvestorProfile, etf: &EtfFacts) -> Evaluation {
        evaluate(spec, profile, etf)
    }

    fn facts(etf_id: &str) -> EtfFacts {
        load_etfs()
            .into_iter()
            .map(|etf| etf.facts())
            .find(|facts| facts.etf_id == etf_id)
            .unwrap_or_else(|| panic!("{etf_id} is not present in etfs.json"))
    }

    // ------------------------------------------------------------ specification

    #[test]
    fn the_shipped_specification_validates() {
        let spec = load_rules();
        assert_eq!(spec.score_components.iter().map(|c| c.weight).sum::<i32>(), 100);
        assert_eq!(spec.decision_order, DECISIONS);
    }

    #[test]
    fn a_specification_whose_weights_do_not_sum_to_100_is_rejected() {
        let mut spec = load_rules();
        spec.score_components[0].weight += 1;
        let error = spec.validate().expect_err("must reject");
        assert!(error.contains("metrics sum to"), "{error}");
    }

    #[test]
    fn a_component_whose_metrics_disagree_with_its_weight_is_rejected() {
        let mut spec = load_rules();
        let component = &mut spec.score_components[1];
        if let Metric::Numeric { weight, .. } = &mut component.metrics[0] {
            *weight += 3;
        }
        let error = spec.validate().expect_err("must reject");
        assert!(error.contains("metrics sum to"), "{error}");
    }

    #[test]
    fn a_metric_scoring_on_an_unknown_field_is_rejected() {
        let mut spec = load_rules();
        if let Metric::Numeric { field, .. } = &mut spec.score_components[0].metrics[0] {
            *field = "expected_return".to_string();
        }
        let error = spec.validate().expect_err("must reject");
        assert!(error.contains("unknown ETF field"), "{error}");
    }

    #[test]
    fn decision_thresholds_must_tile_the_whole_score_range() {
        let mut spec = load_rules();
        spec.decision_thresholds[1].min_score = 51;
        assert!(spec.validate().is_err());

        let mut spec = load_rules();
        spec.decision_thresholds.pop();
        assert!(spec.validate().is_err());
    }

    #[test]
    fn a_specification_with_two_fall_through_bands_is_rejected() {
        let mut spec = load_rules();
        if let Metric::Numeric { bands, .. } = &mut spec.score_components[0].metrics[0] {
            bands[0].threshold = None;
        }
        let error = spec.validate().expect_err("must reject");
        assert!(error.contains("fall-through band"), "{error}");
    }

    // ------------------------------------------------------------------ ordering

    #[test]
    fn decision_ranking_is_ordered_by_attractiveness() {
        assert!(decision_rank("reject") < decision_rank("research"));
        assert!(decision_rank("research") < decision_rank("shortlist"));
        assert_eq!(decision_rank("nonsense"), -1);
        for decision in DECISIONS {
            assert!(
                decision_rank(decision) > decision_rank("nonsense"),
                "an unrecognised value must never outrank a real decision"
            );
        }
    }

    #[test]
    fn decision_normalisation_accepts_only_the_three_decisions() {
        for value in ["reject", "  SHORTLIST ", "Research"] {
            assert!(normalize_decision(value).is_ok(), "{value}");
        }
        // Plausible-looking neighbours, all refused. The vocabulary is closed, and
        // a near-miss must fail loudly rather than resolve to something.
        for value in ["", "shortlist!", "short list", "shortlisted", "rejected", "maybe"] {
            assert!(normalize_decision(value).is_err(), "{value}");
        }
    }

    // ------------------------------------------------------------- score bands

    /// The exact boundaries the README publishes.
    #[test]
    fn score_bands_map_to_decisions_at_their_documented_boundaries() {
        let spec = load_rules();
        let band = |score: i32| spec.threshold_for(score).expect("tiled").decision.clone();
        assert_eq!(band(0), "reject");
        assert_eq!(band(49), "reject");
        assert_eq!(band(50), "research");
        assert_eq!(band(74), "research");
        assert_eq!(band(75), "shortlist");
        assert_eq!(band(100), "shortlist");
    }

    /// Band selection is by bound, not by array position.
    #[test]
    fn band_matching_does_not_depend_on_file_order() {
        let spec = load_rules();
        let profile = load_profile();
        let mut reversed = spec.clone();
        for component in &mut reversed.score_components {
            for metric in &mut component.metrics {
                if let Metric::Numeric { bands, .. } = metric {
                    bands.reverse();
                }
            }
        }
        reversed.score_components.reverse();
        reversed.decision_thresholds.reverse();
        reversed.validate().expect("reordering must stay valid");

        for etf in load_etfs() {
            let facts = etf.facts();
            let original = decide(&spec, &profile, &facts);
            let shuffled = decide(&reversed, &profile, &facts);
            assert_eq!(
                (original.investment_score, original.decision.clone()),
                (shuffled.investment_score, shuffled.decision.clone()),
                "{} changed when rules_spec.json was reordered",
                facts.etf_id
            );
        }
    }

    #[test]
    fn numeric_bands_select_on_the_inclusive_boundary() {
        let bands = vec![
            Band { code: "A".into(), threshold: Some(0.001), fraction: 1.0, note: String::new() },
            Band { code: "B".into(), threshold: Some(0.002), fraction: 0.85, note: String::new() },
            Band { code: "C".into(), threshold: None, fraction: 0.0, note: String::new() },
        ];
        assert_eq!(band_for(&bands, Direction::LowerIsBetter, 0.0005).code, "A");
        assert_eq!(band_for(&bands, Direction::LowerIsBetter, 0.001).code, "A");
        assert_eq!(band_for(&bands, Direction::LowerIsBetter, 0.0011).code, "B");
        assert_eq!(band_for(&bands, Direction::LowerIsBetter, 0.002).code, "B");
        assert_eq!(band_for(&bands, Direction::LowerIsBetter, 0.5).code, "C");

        let bands = vec![
            Band { code: "A".into(), threshold: Some(2000.0), fraction: 1.0, note: String::new() },
            Band { code: "B".into(), threshold: Some(1000.0), fraction: 0.85, note: String::new() },
            Band { code: "C".into(), threshold: None, fraction: 0.0, note: String::new() },
        ];
        assert_eq!(band_for(&bands, Direction::HigherIsBetter, 5000.0).code, "A");
        assert_eq!(band_for(&bands, Direction::HigherIsBetter, 2000.0).code, "A");
        assert_eq!(band_for(&bands, Direction::HigherIsBetter, 1999.0).code, "B");
        assert_eq!(band_for(&bands, Direction::HigherIsBetter, 1000.0).code, "B");
        assert_eq!(band_for(&bands, Direction::HigherIsBetter, 10.0).code, "C");
    }

    // -------------------------------------------------------- labelled fixtures

    /// The labelled cases, through the shipped engine.
    #[test]
    fn labelled_test_cases_all_hold() {
        let spec = load_rules();
        let profile = load_profile();
        let cases = load_test_cases();
        assert!(cases.len() >= 12, "the labelled set must cover the decision space");

        let mut failures = Vec::new();
        for case in &cases {
            let evaluation = decide(&spec, &profile, &facts(&case.etf_id));
            let mut problems = Vec::new();
            if evaluation.decision != case.expected_decision {
                problems.push(format!(
                    "decision {}, expected {}",
                    evaluation.decision, case.expected_decision
                ));
            }
            let [low, high] = case.expected_score_range;
            if evaluation.investment_score < low || evaluation.investment_score > high {
                problems.push(format!(
                    "score {} outside the expected range {low}..={high}",
                    evaluation.investment_score
                ));
            }
            let hit_codes: Vec<&str> =
                evaluation.hard_constraints.iter().map(|hit| hit.code.as_str()).collect();
            for code in &case.expected_hard_constraints {
                if !hit_codes.contains(&code.as_str()) {
                    problems.push(format!("hard constraint {code} did not fire"));
                }
            }
            if case.expected_hard_constraints.is_empty() && !hit_codes.is_empty() {
                problems.push(format!("unexpected hard constraints {hit_codes:?}"));
            }
            let cap_codes: Vec<&str> =
                evaluation.applied_caps.iter().map(|cap| cap.code.as_str()).collect();
            for code in &case.expected_caps {
                if !cap_codes.contains(&code.as_str()) {
                    problems.push(format!("cap {code} was not applied"));
                }
            }
            if case.expected_caps.is_empty() && !cap_codes.is_empty() {
                problems.push(format!("unexpected caps {cap_codes:?}"));
            }
            let missing: Vec<&str> =
                evaluation.missing_data.iter().map(|entry| entry.field.as_str()).collect();
            for field in &case.expected_missing_data {
                if !missing.contains(&field.as_str()) {
                    problems.push(format!("{field} was expected to be missing"));
                }
            }
            let rule_codes: Vec<&str> =
                evaluation.matched_rules.iter().map(|rule| rule.code.as_str()).collect();
            for code in &case.expected_rule_codes {
                if !rule_codes.contains(&code.as_str()) {
                    problems.push(format!("rule {code} did not match"));
                }
            }
            if !problems.is_empty() {
                failures.push(format!(
                    "{} ({}): {} [{}]",
                    case.case_id,
                    case.etf_id,
                    problems.join("; "),
                    case.rationale
                ));
            }
        }
        assert!(
            failures.is_empty(),
            "{} of {} labelled cases failed:\n{}",
            failures.len(),
            cases.len(),
            failures.join("\n")
        );
    }

    #[test]
    fn every_labelled_case_names_a_real_etf_and_a_real_decision() {
        let etf_ids: BTreeSet<String> =
            load_etfs().into_iter().map(|etf| etf.etf_id).collect();
        for case in load_test_cases() {
            assert!(etf_ids.contains(&case.etf_id), "{} names no ETF", case.case_id);
            assert!(
                DECISIONS.contains(&case.expected_decision.as_str()),
                "{} expects an unknown decision",
                case.case_id
            );
            assert!(!case.rationale.trim().is_empty(), "{} has no rationale", case.case_id);
        }
    }

    // ------------------------------------------------------ engine invariants

    #[test]
    fn every_shipped_etf_scores_inside_the_published_range() {
        let spec = load_rules();
        let profile = load_profile();
        let etfs = load_etfs();
        assert!(etfs.len() >= 20, "the snapshot must be large enough to exercise the policy");

        for etf in etfs {
            let facts = etf.facts();
            let evaluation = decide(&spec, &profile, &facts);
            assert!(
                (0..=100).contains(&evaluation.investment_score),
                "{} scored {}",
                facts.etf_id,
                evaluation.investment_score
            );
            assert!(
                DECISIONS.contains(&evaluation.decision.as_str()),
                "{} produced an unknown decision {}",
                facts.etf_id,
                evaluation.decision
            );
            assert!(
                (0.0..=1.0).contains(&evaluation.data_completeness),
                "{} reported completeness {}",
                facts.etf_id,
                evaluation.data_completeness
            );
        }
    }

    /// The reported components must sum to the published score, or the score is
    /// not explainable by them.
    #[test]
    fn reported_components_sum_to_the_published_score() {
        let spec = load_rules();
        let profile = load_profile();
        for etf in load_etfs() {
            let facts = etf.facts();
            let evaluation = decide(&spec, &profile, &facts);
            let summed: i32 = evaluation.components.values().sum();
            assert_eq!(
                summed, evaluation.investment_score,
                "{} components sum to {summed} but the score is {}",
                facts.etf_id, evaluation.investment_score
            );
        }
    }

    /// The four reported numbers must reconcile, and the one that can exceed its
    /// nominal weight must be the *normalised* one — never the raw earnings.
    #[test]
    fn the_component_breakdown_publishes_the_renormalisation() {
        let spec = load_rules();
        let profile = load_profile();
        for etf in load_etfs() {
            let evaluation = decide(&spec, &profile, &etf.facts());
            let normalization = &evaluation.normalization;
            assert_eq!(normalization.total_weight, 100);
            assert_eq!(
                normalization.available_weight,
                evaluation
                    .component_breakdown
                    .iter()
                    .map(|entry| entry.available_weight)
                    .sum::<i32>()
            );
            let contributions: i32 = evaluation
                .component_breakdown
                .iter()
                .map(|entry| entry.normalized_contribution)
                .sum();
            assert_eq!(contributions, evaluation.investment_score, "{}", etf.etf_id);

            for entry in &evaluation.component_breakdown {
                assert!(
                    entry.available_weight <= entry.nominal_weight,
                    "{} {} scored on more weight than it declares",
                    etf.etf_id,
                    entry.key
                );
                assert!(
                    entry.raw_earned_points <= f64::from(entry.available_weight) + 1e-9,
                    "{} {} earned {} raw points out of {} available",
                    etf.etf_id,
                    entry.key,
                    entry.raw_earned_points,
                    entry.available_weight
                );
                assert_eq!(entry.unavailable, entry.available_weight == 0);
                if entry.unavailable {
                    assert_eq!(entry.raw_earned_points, 0.0);
                    assert_eq!(entry.normalized_contribution, 0);
                }
            }
        }
    }

    /// The confusing case, made legible rather than removed: with weight missing
    /// elsewhere, a fully scored component contributes more than its nominal
    /// weight, and the evaluation says so in numbers a reader can check.
    #[test]
    fn a_renormalised_contribution_may_exceed_its_nominal_weight_and_is_explained() {
        let spec = load_rules();
        let profile = load_profile();
        let evaluation = decide(&spec, &profile, &facts("VWCE-XETRA"));
        assert!(
            evaluation.normalization.available_weight < evaluation.normalization.total_weight,
            "the shipped snapshot has no realised tracking data, so weight must be withdrawn"
        );
        assert!(evaluation.normalization.factor > 1.0);

        let cost = evaluation
            .component_breakdown
            .iter()
            .find(|entry| entry.key == "cost_efficiency")
            .expect("cost_efficiency is a shipped component");
        assert_eq!(cost.available_weight, cost.nominal_weight);
        assert!(
            f64::from(cost.normalized_contribution) >= cost.raw_earned_points,
            "renormalisation scales contributions up, never down"
        );
        assert!(evaluation.explanation.contains("renormalised away"));
        assert!(evaluation.normalization.note.contains("not a component earning more"));
    }

    /// Structure is not evidence of tracking fidelity, and the split says so.
    #[test]
    fn tracking_quality_is_unavailable_across_the_whole_snapshot() {
        let spec = load_rules();
        let profile = load_profile();
        for etf in load_etfs() {
            let evaluation = decide(&spec, &profile, &etf.facts());
            let tracking = evaluation
                .component_breakdown
                .iter()
                .find(|entry| entry.key == "tracking_quality")
                .expect("tracking_quality is a shipped component");
            assert!(
                tracking.unavailable,
                "{} reported observed tracking quality it does not have",
                etf.etf_id
            );
            assert!(evaluation.explanation.contains("Unavailable components"));

            // The replication method is still scored, under its own name.
            let structure = evaluation
                .component_breakdown
                .iter()
                .find(|entry| entry.key == "fund_structure")
                .expect("fund_structure is a shipped component");
            assert!(!structure.unavailable, "{}", etf.etf_id);
        }
    }

    #[test]
    fn evaluation_is_deterministic_across_repeated_calls() {
        let spec = load_rules();
        let profile = load_profile();
        for etf in load_etfs() {
            let facts = etf.facts();
            let first = serde_json::to_string(&decide(&spec, &profile, &facts)).expect("serialise");
            let second = serde_json::to_string(&decide(&spec, &profile, &facts)).expect("serialise");
            assert_eq!(first, second, "{}", facts.etf_id);
        }
    }

    /// Text matching must not depend on the casing an upstream feed happens to
    /// use. This asymmetry is the dangerous kind: a mixed-case value that still
    /// resolves to the right band while a stricter comparison elsewhere silently
    /// fails to fire.
    #[test]
    fn text_matching_is_case_insensitive_in_every_direction() {
        let spec = load_rules();
        let profile = load_profile();
        let canonical = facts("VWCE-XETRA");
        let shouted = EtfFacts {
            asset_class: canonical.asset_class.to_ascii_uppercase(),
            region: canonical.region.to_ascii_uppercase(),
            distribution_policy: "Accumulating".to_string(),
            replication: "PHYSICAL".to_string(),
            ..canonical.clone()
        };
        let expected = decide(&spec, &profile, &canonical);
        let actual = decide(&spec, &profile, &shouted);
        assert_eq!(expected.investment_score, actual.investment_score);
        assert_eq!(expected.decision, actual.decision);
    }

    #[test]
    fn seeded_text_values_are_canonical() {
        for etf in load_etfs() {
            for (name, value) in [
                ("asset_class", &etf.asset_class),
                ("region", &etf.region),
                ("distribution_policy", &etf.distribution_policy),
                ("replication", &etf.replication),
            ] {
                assert_eq!(
                    value.as_str(),
                    value.to_ascii_lowercase().as_str(),
                    "{} stores a non-canonical {name}",
                    etf.etf_id
                );
            }
        }
    }

    #[test]
    fn etf_ids_are_unique_and_tickers_are_not_assumed_to_be() {
        let etfs = load_etfs();
        let ids: BTreeSet<&str> = etfs.iter().map(|etf| etf.etf_id.as_str()).collect();
        assert_eq!(ids.len(), etfs.len(), "etf_id must be unique");
        // The snapshot deliberately contains one fund cross-listed under the same
        // ticker and ISIN, so anything keyed on ticker alone is a bug.
        let tickers: Vec<&str> = etfs.iter().map(|etf| etf.ticker.as_str()).collect();
        let unique_tickers: BTreeSet<&str> = tickers.iter().copied().collect();
        assert!(
            unique_tickers.len() < tickers.len(),
            "the snapshot must keep a cross-listed ticker so ticker-keyed lookups cannot pass by luck"
        );
    }

    // ------------------------------------------------------- hard constraints

    #[test]
    fn a_non_ucits_fund_is_rejected_however_well_it_scores() {
        let spec = load_rules();
        let profile = load_profile();
        // Whole-market United States equity at three basis points: the highest
        // score of any rejected fund in the snapshot, and rejected regardless.
        let evaluation = decide(&spec, &profile, &facts("VTI-ARCA"));
        assert_eq!(evaluation.decision, "reject");
        assert_eq!(evaluation.score_decision, "shortlist");
        assert!(evaluation.investment_score >= 75, "{}", evaluation.investment_score);
        assert_eq!(evaluation.hard_constraints.len(), 1);
        assert_eq!(evaluation.hard_constraints[0].code, "HC-UCITS");
        assert!(!evaluation.hard_constraints[0].bypassable);
    }

    #[test]
    fn every_non_ucits_fund_in_the_snapshot_is_rejected() {
        let spec = load_rules();
        let profile = load_profile();
        let mut seen = 0;
        for etf in load_etfs() {
            if etf.ucits {
                continue;
            }
            seen += 1;
            let evaluation = decide(&spec, &profile, &etf.facts());
            assert_eq!(evaluation.decision, "reject", "{}", etf.etf_id);
        }
        assert!(seen >= 3, "the snapshot must contain non-UCITS funds to constrain");
    }

    #[test]
    fn a_hard_constraint_only_applies_when_the_profile_enables_it() {
        let spec = load_rules();
        let mut profile = load_profile();
        profile.hard_constraints.insert("require_ucits".to_string(), json!(false));
        let evaluation = decide(&spec, &profile, &facts("VTI-ARCA"));
        assert!(evaluation.hard_constraints.is_empty());
        assert_eq!(evaluation.decision, evaluation.score_decision);
        assert_eq!(evaluation.decision, "shortlist");
    }

    /// The mutation-time gate: no actor may lift a non-bypassable constraint.
    #[test]
    fn a_non_bypassable_constraint_blocks_every_decision_above_reject() {
        let spec = load_rules();
        let profile = load_profile();
        let non_ucits = facts("VTI-ARCA");
        for decision in ["research", "shortlist"] {
            for override_applied in [false, true] {
                let blocked =
                    blocking_hard_constraint(&spec, &profile, &non_ucits, decision, override_applied);
                assert!(
                    blocked.is_some(),
                    "{decision} was permitted with override_applied={override_applied}"
                );
            }
        }
        assert!(blocking_hard_constraint(&spec, &profile, &non_ucits, "reject", false).is_none());

        let ucits = facts("VWCE-XETRA");
        for decision in DECISIONS {
            assert!(blocking_hard_constraint(&spec, &profile, &ucits, decision, false).is_none());
        }
    }

    /// A record that cannot answer a hard constraint must not satisfy it.
    #[test]
    fn a_constraint_field_that_cannot_be_compared_does_not_match() {
        let mut spec = load_rules();
        spec.hard_constraints[0].field = "replication".to_string();
        let profile = load_profile();
        let hits = hard_constraint_hits(&spec, &profile, &facts("VWCE-XETRA"));
        assert_eq!(hits.len(), 1, "a boolean requirement against text must not pass");
    }

    // ---------------------------------------------------------- missing data

    #[test]
    fn absent_metrics_leave_the_denominator_rather_than_scoring_zero() {
        let spec = load_rules();
        let profile = load_profile();
        let complete = facts("VWCE-XETRA");
        let stripped = EtfFacts { holdings_count: None, ..complete.clone() };

        let full = decide(&spec, &profile, &complete);
        let partial = decide(&spec, &profile, &stripped);
        assert_eq!(
            partial.normalization.available_weight,
            full.normalization.available_weight - 12
        );
        assert!(
            partial.missing_data.iter().any(|entry| entry.field == "holdings_count"),
            "the absence must be reported"
        );
        // Scoring it as zero would have dropped the score by roughly 12 points.
        // Renormalising leaves it close to the complete record.
        assert!(
            (partial.investment_score - full.investment_score).abs() <= 3,
            "{} vs {}",
            partial.investment_score,
            full.investment_score
        );
    }

    #[test]
    fn a_missing_critical_field_caps_the_decision_at_research() {
        let spec = load_rules();
        let profile = load_profile();
        let complete = facts("VWCE-XETRA");
        assert_eq!(decide(&spec, &profile, &complete).decision, "shortlist");

        for stripped in [
            EtfFacts { ter: None, ..complete.clone() },
            EtfFacts { aum_usd: None, ..complete.clone() },
            EtfFacts { fund_age_years: None, ..complete.clone() },
            EtfFacts { holdings_count: None, ..complete.clone() },
            EtfFacts { top_10_concentration: None, ..complete.clone() },
        ] {
            let evaluation = decide(&spec, &profile, &stripped);
            assert_eq!(
                evaluation.decision, "research",
                "an incomplete record reached {} with score {}",
                evaluation.decision, evaluation.investment_score
            );
            assert!(
                evaluation
                    .applied_caps
                    .iter()
                    .any(|cap| cap.code == "CAP-CRITICAL-DATA"),
                "the cap must be published, not merely applied"
            );
            assert!(evaluation.explanation.contains("Missing critical fields"));
        }
    }

    /// The published example: an otherwise strong global bond fund whose
    /// concentration figure the issuer does not publish.
    #[test]
    fn the_shipped_snapshot_contains_a_capped_shortlist() {
        let spec = load_rules();
        let profile = load_profile();
        let evaluation = decide(&spec, &profile, &facts("AGGH-XETRA"));
        assert_eq!(evaluation.score_decision, "shortlist");
        assert_eq!(evaluation.decision, "research");
        assert!(evaluation.applied_caps.iter().any(|cap| cap.code == "CAP-CRITICAL-DATA"));
    }

    /// A cap can only ever make a decision less attractive.
    #[test]
    fn caps_never_promote_a_decision() {
        let spec = load_rules();
        let profile = load_profile();
        for etf in load_etfs() {
            let evaluation = decide(&spec, &profile, &etf.facts());
            if evaluation.hard_constraints.is_empty() {
                assert!(
                    decision_rank(&evaluation.decision) <= decision_rank(&evaluation.score_decision),
                    "{} was promoted from {} to {}",
                    etf.etf_id,
                    evaluation.score_decision,
                    evaluation.decision
                );
            }
        }
    }

    #[test]
    fn a_record_with_no_scorable_data_scores_zero_and_rejects() {
        let spec = load_rules();
        let profile = load_profile();
        let empty = EtfFacts { etf_id: "EMPTY".to_string(), ucits: true, ..EtfFacts::default() };
        let evaluation = decide(&spec, &profile, &empty);
        assert_eq!(evaluation.investment_score, 0);
        assert_eq!(evaluation.decision, "reject");
        assert!(evaluation.components.values().all(|points| *points == 0));
    }

    /// An unrecognised value is a data defect, not evidence of zero quality.
    ///
    /// The two metrics that read `replication` treat it differently on purpose.
    /// The categorical *quality* metric cannot place an unknown method on its
    /// scale, so it withdraws its weight and reports the value. The *preference*
    /// metric is a membership test — the investor asked for physical or sampled
    /// replication — and an unknown method demonstrably is not one of those, so
    /// it is scored as unmet rather than excused.
    #[test]
    fn an_unrecognised_categorical_value_is_reported_not_scored() {
        let spec = load_rules();
        let profile = load_profile();
        let baseline = decide(&spec, &profile, &facts("VWCE-XETRA"));
        let odd = EtfFacts { replication: "quantum".to_string(), ..facts("VWCE-XETRA") };
        let evaluation = decide(&spec, &profile, &odd);

        let reported: Vec<&MissingDatum> = evaluation
            .missing_data
            .iter()
            .filter(|entry| entry.field == "replication")
            .collect();
        assert_eq!(reported.len(), 1, "the quality metric must report the unusable value");
        assert_eq!(reported[0].metric, "replication");
        assert!(reported[0].reason.contains("vocabulary"), "{}", reported[0].reason);
        assert!(!reported[0].critical, "replication is not a critical field");
        assert_eq!(
            evaluation.normalization.available_weight,
            baseline.normalization.available_weight - 9,
            "the unscorable structure metric must withdraw its weight"
        );

        let preference = evaluation
            .matched_rules
            .iter()
            .find(|rule| rule.metric == "physical_replication")
            .expect("the preference is still evaluated");
        assert_eq!(preference.code, "PREF-PHYS-UNMET");
        assert_eq!(preference.fraction, 0.0);
    }

    // -------------------------------------------------------- profile effects

    #[test]
    fn switching_off_a_preference_removes_its_weight_rather_than_penalising_every_fund() {
        let spec = load_rules();
        let mut profile = load_profile();
        let distributing = facts("VWRL-LSE");
        let with_preference = decide(&spec, &profile, &distributing);

        profile.preferences.insert("accumulating".to_string(), false);
        let without = decide(&spec, &profile, &distributing);

        assert_eq!(
            without.normalization.available_weight,
            with_preference.normalization.available_weight - 4
        );
        assert!(without.not_applicable.iter().any(|entry| entry.metric == "accumulating"));
        assert!(
            without.investment_score > with_preference.investment_score,
            "removing an unmet preference must not leave the fund worse off: {} vs {}",
            without.investment_score,
            with_preference.investment_score
        );
    }

    #[test]
    fn a_different_risk_tolerance_changes_the_evaluation() {
        let spec = load_rules();
        let mut profile = load_profile();
        let bond_fund = facts("IEAC-LSE");
        let for_high = decide(&spec, &profile, &bond_fund);

        profile.risk_tolerance = "low".to_string();
        let for_low = decide(&spec, &profile, &bond_fund);
        assert!(
            for_low.profile_fit.fraction > for_high.profile_fit.fraction,
            "a corporate bond fund must fit a low-risk profile better: {} vs {}",
            for_low.profile_fit.fraction,
            for_high.profile_fit.fraction
        );
    }

    /// A high-quality fund whose asset class does not suit the profile must not
    /// shortlist on quality alone. This is the distinction the whole project is
    /// about.
    #[test]
    fn quality_without_fit_is_capped_at_research() {
        let spec = load_rules();
        let profile = load_profile();
        let evaluation = decide(&spec, &profile, &facts("IEAC-LSE"));
        assert!(evaluation.profile_fit.fraction < 0.5, "{:?}", evaluation.profile_fit);
        assert!(evaluation.applied_caps.iter().any(|cap| cap.code == "CAP-PROFILE-FIT"));
        assert_eq!(evaluation.decision, "research");
    }

    #[test]
    fn the_profile_version_travels_with_every_evaluation() {
        let spec = load_rules();
        let profile = load_profile();
        let evaluation = decide(&spec, &profile, &facts("VWCE-XETRA"));
        assert_eq!(evaluation.rules_version, spec.version);
        assert_eq!(evaluation.profile_version, profile.version);
        assert!(evaluation.note.contains("not an expected-return forecast"));
    }

    // ------------------------------------------------------- override policy

    #[test]
    fn a_model_may_be_conservative_and_may_never_be_optimistic() {
        for (deterministic, recommendation, allowed, relationship) in [
            ("shortlist", "research", true, "more_conservative"),
            ("shortlist", "reject", true, "more_conservative"),
            ("research", "reject", true, "more_conservative"),
            ("shortlist", "shortlist", true, "equal"),
            ("research", "research", true, "equal"),
            ("reject", "reject", true, "equal"),
            ("research", "shortlist", false, "more_optimistic"),
            ("reject", "research", false, "more_optimistic"),
            ("reject", "shortlist", false, "more_optimistic"),
        ] {
            let comparison = compare_recommendation(deterministic, recommendation);
            assert_eq!(comparison.allowed, allowed, "{deterministic} vs {recommendation}");
            assert_eq!(comparison.relationship, relationship);
            // Permitted is not adopted. Even an allowed, more-conservative
            // recommendation leaves the deterministic decision standing.
            assert_eq!(comparison.default_decision, deterministic);
            assert_eq!(comparison.deterministic_decision, deterministic);
            assert_eq!(comparison.policy_violation.is_some(), !allowed);
        }
    }

    // ------------------------------------------------------ decision authority

    fn authority(
        rules_decision: &str,
        llm: Option<&str>,
        requested: &str,
        override_requested: bool,
    ) -> Result<DecisionAuthority, String> {
        reconcile_decision(rules_decision, llm, requested, override_requested)
    }

    /// Case A. The model counselled caution and the human took the engine's
    /// decision. That is *not* an override: the model never held the default.
    #[test]
    fn choosing_the_deterministic_decision_over_a_conservative_model_is_not_an_override() {
        let outcome = authority("shortlist", Some("research"), "shortlist", false)
            .expect("the deterministic decision is always available without an override");
        assert_eq!(outcome.default_decision, "shortlist");
        assert!(!outcome.override_applied);
        assert_eq!(outcome.human_override_decision, None);
        assert_eq!(outcome.llm_recommendation.as_deref(), Some("research"));

        // ...and claiming it as one is refused, so the flag cannot be smuggled in.
        let error = authority("shortlist", Some("research"), "shortlist", true)
            .expect_err("an unchanged decision is not an override");
        assert!(error.contains("override flag does not match"), "{error}");
    }

    /// Case B. The human adopted the model's more conservative view. That *is* an
    /// override, because it moves away from the deterministic decision.
    #[test]
    fn following_a_conservative_model_away_from_the_engine_is_a_human_override() {
        let outcome = authority("shortlist", Some("research"), "research", true)
            .expect("a downward human override is permitted");
        assert!(outcome.override_applied);
        assert_eq!(outcome.human_override_decision.as_deref(), Some("research"));
        assert_eq!(outcome.default_decision, "shortlist");

        let error = authority("shortlist", Some("research"), "research", false)
            .expect_err("a changed decision must declare itself an override");
        assert!(error.contains("override flag does not match"), "{error}");
    }

    /// Case C. A more optimistic model proposal is refused outright, before any
    /// mutation, whatever the human then chose.
    #[test]
    fn a_more_optimistic_model_recommendation_is_refused_before_anything_else() {
        for (requested, override_requested) in
            [("research", false), ("shortlist", true), ("reject", true)]
        {
            let error = authority("research", Some("shortlist"), requested, override_requested)
                .expect_err("a model promotion must never reach a mutation");
            assert!(error.contains("more optimistic"), "{error}");
        }
    }

    /// Case D. A human promotion above the engine is representable, and is
    /// recorded as an override.
    #[test]
    fn a_human_may_move_above_the_deterministic_decision() {
        let outcome =
            authority("research", None, "shortlist", true).expect("a human promotion is allowed");
        assert!(outcome.override_applied);
        assert_eq!(outcome.human_override_decision.as_deref(), Some("shortlist"));
        assert_eq!(outcome.default_decision, "research");
        assert_eq!(outcome.llm_recommendation, None);
    }

    /// Case E. A non-bypassable constraint is a separate gate, and it holds
    /// against a fully valid human override.
    #[test]
    fn a_human_override_cannot_reach_past_a_non_bypassable_constraint() {
        let spec = load_rules();
        let profile = load_profile();
        let non_ucits = facts("VTI-ARCA");
        let outcome = authority("reject", None, "shortlist", true).expect("representable");
        assert!(outcome.override_applied);
        let blocked = blocking_hard_constraint(
            &spec,
            &profile,
            &non_ucits,
            &outcome.requested_decision,
            outcome.override_applied,
        )
        .expect("the constraint must still block the approved decision");
        assert_eq!(blocked.code, "HC-UCITS");
        assert!(!blocked.bypassable);
    }

    #[test]
    fn reconciliation_refuses_anything_outside_the_decision_vocabulary() {
        assert!(authority("research", None, "approve", false).is_err());
        assert!(authority("maybe", None, "research", false).is_err());
        assert!(authority("research", Some("escalate"), "research", false).is_err());
    }

    #[test]
    fn the_shipped_snapshot_exercises_every_decision() {
        let spec = load_rules();
        let profile = load_profile();
        let mut seen: BTreeMap<String, usize> = BTreeMap::new();
        for etf in load_etfs() {
            *seen.entry(decide(&spec, &profile, &etf.facts()).decision).or_default() += 1;
        }
        for decision in DECISIONS {
            assert!(
                seen.get(decision).copied().unwrap_or(0) > 0,
                "no ETF in the snapshot produces {decision}: {seen:?}"
            );
        }
    }

    // -------------------------------------------------------------- baseline

    /// Emit the checked-in deterministic baseline from the shipped engine.
    ///
    /// Generating it here means the published numbers and the deployed code
    /// cannot diverge: regenerating the artifact is `cargo test`, and any engine
    /// or fixture change rewrites the evidence in the same run that verifies it.
    #[test]
    fn emit_deterministic_baseline() {
        let spec = load_rules();
        let profile = load_profile();
        let etfs = load_etfs();
        let cases = load_test_cases();

        let evaluations: Vec<Value> = etfs
            .iter()
            .map(|etf| {
                let evaluation = decide(&spec, &profile, &etf.facts());
                json!({
                    "etf_id": etf.etf_id,
                    "ticker": etf.ticker,
                    "name": etf.name,
                    "investment_score": evaluation.investment_score,
                    "decision": evaluation.decision,
                    "score_decision": evaluation.score_decision,
                    "components": evaluation.components,
                    "available_weight": evaluation.normalization.available_weight,
                    "unavailable_components": evaluation
                        .component_breakdown
                        .iter()
                        .filter(|entry| entry.unavailable)
                        .map(|entry| entry.key.clone())
                        .collect::<Vec<_>>(),
                    "data_completeness": evaluation.data_completeness,
                    "profile_fit_fraction": evaluation.profile_fit.fraction,
                    "hard_constraints": evaluation
                        .hard_constraints
                        .iter()
                        .map(|hit| hit.code.clone())
                        .collect::<Vec<_>>(),
                    "applied_caps": evaluation
                        .applied_caps
                        .iter()
                        .map(|cap| cap.code.clone())
                        .collect::<Vec<_>>(),
                    "missing_data": evaluation
                        .missing_data
                        .iter()
                        .map(|entry| entry.field.clone())
                        .collect::<Vec<_>>(),
                })
            })
            .collect();

        let case_results: Vec<Value> = cases
            .iter()
            .map(|case| {
                let evaluation = decide(&spec, &profile, &facts(&case.etf_id));
                let [low, high] = case.expected_score_range;
                let passed = evaluation.decision == case.expected_decision
                    && (low..=high).contains(&evaluation.investment_score);
                json!({
                    "case_id": case.case_id,
                    "etf_id": case.etf_id,
                    "expected_decision": case.expected_decision,
                    "actual_decision": evaluation.decision,
                    "expected_score_range": case.expected_score_range,
                    "actual_score": evaluation.investment_score,
                    "passed": passed,
                    "rationale": case.rationale,
                })
            })
            .collect();

        let hard_constraint_failures: Vec<String> = etfs
            .iter()
            .filter_map(|etf| {
                let facts = etf.facts();
                let evaluation = decide(&spec, &profile, &facts);
                blocking_hard_constraint(&spec, &profile, &facts, &evaluation.decision, false)
                    .map(|hit| format!("{}: {}", etf.etf_id, hit.code))
            })
            .collect();

        // Record which fixture bytes produced this result, so the artifact
        // self-certifies what it was measured against.
        let fixture_sha256: serde_json::Map<String, Value> =
            ["etfs.json", "investor_profile.json", "rules_spec.json", "test_cases.json"]
                .into_iter()
                .map(|name| {
                    let bytes =
                        fs::read(repo_root().join("data").join(name)).expect("read fixture");
                    let digest = Sha256::digest(&bytes);
                    let hex =
                        digest.iter().map(|byte| format!("{byte:02x}")).collect::<String>();
                    (name.to_string(), Value::String(hex))
                })
                .collect();

        let passed = case_results.iter().filter(|result| result["passed"] == json!(true)).count();
        let mut by_decision: BTreeMap<String, usize> = BTreeMap::new();
        for evaluation in &evaluations {
            *by_decision
                .entry(evaluation["decision"].as_str().unwrap_or_default().to_string())
                .or_default() += 1;
        }

        let summary = json!({
            "engine": "mcp-server/src/rules.rs evaluate (the shipped Rust implementation)",
            "generated_by": "cargo test -p etf-mcp-server (rules::tests::emit_deterministic_baseline)",
            "score_meaning": SCORE_MEANING,
            "rules_version": spec.version,
            "investor_profile_id": profile.profile_id,
            "investor_profile_version": profile.version,
            "fixture_sha256": fixture_sha256,
            "etf_count": etfs.len(),
            "test_case_count": cases.len(),
            "test_cases_passed": passed,
            "test_cases_failed": case_results.len() - passed,
            "accuracy": passed as f64 / case_results.len() as f64,
            "decision_distribution": by_decision,
            "hard_constraint_failures": hard_constraint_failures,
            "test_cases": case_results,
            "evaluations": evaluations,
        });

        let path = repo_root().join("evaluation/results/deterministic-etf-baseline.json");
        fs::create_dir_all(path.parent().expect("results directory")).expect("create results dir");
        fs::write(
            &path,
            format!("{}\n", serde_json::to_string_pretty(&summary).expect("serialise baseline")),
        )
        .expect("write baseline");

        assert_eq!(passed, case_results.len(), "baseline emitted with failing cases");
        assert!(hard_constraint_failures.is_empty(), "{hard_constraint_failures:?}");
    }
}
