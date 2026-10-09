//! Strict tool-argument schemas.
//!
//! A tool's JSON Schema arrives from the MCP server at discovery time. This
//! module turns it into a typed, closed representation and validates every
//! model-proposed argument object against it *before* anything is sent.
//!
//! Deliberately a small, strict subset rather than a general JSON Schema
//! engine, for two reasons:
//!
//! * **Fail closed at discovery.** A schema using a keyword this module does
//!   not understand (`oneOf`, `$ref`, `pattern`, …) is refused, and the tool is
//!   not exposed. A general validator would silently accept a constraint it
//!   then fails to enforce in the way the server expects.
//! * **Closed objects.** Unknown top-level arguments are always rejected,
//!   whether or not the schema says `additionalProperties: false`. The model
//!   gains no authority by inventing a field — and identity or approval
//!   fields cannot be smuggled in next to legitimate ones.
//!
//! The MCP server stays authoritative: it deserialises arguments into its own
//! typed structs and refuses what they do not accept. This is defence in depth
//! that turns a bad call into a precise message the model can correct,
//! before a round trip — never a substitute for the server's own check.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Map, Value};

/// Bound on any string argument that declares no `maxLength`.
pub const DEFAULT_MAX_STRING_CHARS: usize = 4_096;

/// Argument names no tool may accept. A tool whose schema declares one is not
/// exposed at all: identity, request correlation and approval evidence come
/// from trusted request context, never from model-produced arguments.
pub const RESERVED_ARGUMENT_NAMES: &[&str] = &[
    "actor_id",
    "user_id",
    "userid",
    "user",
    "username",
    "email",
    "roles",
    "role",
    "request_id",
    "approval",
    "approved",
    "approval_token",
    "approval_id",
    "token",
    "nonce",
    "signature",
    "interaction_id",
    "execution_id",
    "override_requested",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum JsonType {
    String,
    Integer,
    Number,
    Boolean,
    Null,
}

impl JsonType {
    fn parse(name: &str) -> Option<Self> {
        Some(match name {
            "string" => Self::String,
            "integer" => Self::Integer,
            "number" => Self::Number,
            "boolean" => Self::Boolean,
            "null" => Self::Null,
            _ => return None,
        })
    }

    fn name(self) -> &'static str {
        match self {
            Self::String => "string",
            Self::Integer => "integer",
            Self::Number => "number",
            Self::Boolean => "boolean",
            Self::Null => "null",
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct PropertySchema {
    pub types: BTreeSet<JsonType>,
    pub enumeration: Option<Vec<Value>>,
    pub min_length: Option<usize>,
    pub max_length: usize,
    pub minimum: Option<f64>,
    pub maximum: Option<f64>,
}

/// A tool's argument object, closed.
#[derive(Debug, Clone, PartialEq)]
pub struct ObjectSchema {
    pub properties: BTreeMap<String, PropertySchema>,
    pub required: BTreeSet<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum SchemaError {
    #[error("the tool's input schema is not an object schema")]
    NotAnObject,
    #[error("unsupported schema keyword {0:?}")]
    UnsupportedKeyword(String),
    #[error("unsupported schema type {0:?}")]
    UnsupportedType(String),
    #[error("argument {0:?} is reserved for trusted request context")]
    ReservedArgument(String),
    #[error("required argument {0:?} is not declared")]
    UndeclaredRequired(String),
    #[error("malformed schema: {0}")]
    Malformed(String),
}

/// Why a proposed argument object was refused. The text is safe to show the
/// model: it names fields and expectations, never values.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ArgumentError {
    #[error("arguments must be a JSON object")]
    NotAnObject,
    #[error("unknown argument {0:?}")]
    Unknown(String),
    #[error("missing required argument {0:?}")]
    Missing(String),
    #[error("argument {name:?} must be {expected}")]
    WrongType { name: String, expected: String },
    #[error("argument {0:?} must be one of the allowed values")]
    NotInEnum(String),
    #[error("argument {name:?} has the wrong length (allowed {min}..={max} characters)")]
    Length { name: String, min: usize, max: usize },
    #[error("argument {0:?} is out of range")]
    Range(String),
}

const OBJECT_KEYWORDS: &[&str] =
    &["type", "properties", "required", "additionalProperties", "$schema", "title", "description"];
const PROPERTY_KEYWORDS: &[&str] = &[
    "type",
    "description",
    "title",
    "format",
    "enum",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "default",
    "nullable",
];
const FORMATS: &[&str] =
    &["int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64", "uint", "float", "double"];

impl ObjectSchema {
    /// Parse a tool input schema, refusing anything outside the subset.
    pub fn parse(schema: &Value) -> Result<Self, SchemaError> {
        let object = schema.as_object().ok_or(SchemaError::NotAnObject)?;
        for key in object.keys() {
            if !OBJECT_KEYWORDS.contains(&key.as_str()) {
                return Err(SchemaError::UnsupportedKeyword(key.clone()));
            }
        }
        if object.get("type").and_then(Value::as_str) != Some("object") {
            return Err(SchemaError::NotAnObject);
        }
        // `additionalProperties: true` is not honoured: the object is closed
        // regardless. Anything other than a boolean is a sub-schema, refused.
        if let Some(additional) = object.get("additionalProperties")
            && !additional.is_boolean()
        {
            return Err(SchemaError::UnsupportedKeyword("additionalProperties".into()));
        }

        let mut properties = BTreeMap::new();
        if let Some(raw) = object.get("properties") {
            let raw = raw.as_object().ok_or_else(|| SchemaError::Malformed("properties".into()))?;
            for (name, property) in raw {
                if RESERVED_ARGUMENT_NAMES.contains(&name.to_ascii_lowercase().as_str()) {
                    return Err(SchemaError::ReservedArgument(name.clone()));
                }
                properties.insert(name.clone(), PropertySchema::parse(name, property)?);
            }
        }

        let mut required = BTreeSet::new();
        if let Some(raw) = object.get("required") {
            let raw = raw.as_array().ok_or_else(|| SchemaError::Malformed("required".into()))?;
            for name in raw {
                let name = name.as_str().ok_or_else(|| SchemaError::Malformed("required".into()))?;
                if !properties.contains_key(name) {
                    return Err(SchemaError::UndeclaredRequired(name.to_string()));
                }
                required.insert(name.to_string());
            }
        }
        Ok(Self { properties, required })
    }

    /// Validate one proposed argument object. Returns the object unchanged on
    /// success: nothing is coerced, so what is validated is what is sent.
    pub fn validate(&self, args: &Value) -> Result<Map<String, Value>, ArgumentError> {
        let object = args.as_object().ok_or(ArgumentError::NotAnObject)?;
        for name in object.keys() {
            if !self.properties.contains_key(name) {
                return Err(ArgumentError::Unknown(name.clone()));
            }
        }
        for name in &self.required {
            match object.get(name) {
                None => return Err(ArgumentError::Missing(name.clone())),
                // A required, non-nullable property sent as null is missing.
                Some(Value::Null) if !self.properties[name].types.contains(&JsonType::Null) => {
                    return Err(ArgumentError::Missing(name.clone()));
                }
                Some(_) => {}
            }
        }
        for (name, value) in object {
            self.properties[name].check(name, value)?;
        }
        Ok(object.clone())
    }
}

impl PropertySchema {
    fn parse(name: &str, raw: &Value) -> Result<Self, SchemaError> {
        let object = raw.as_object().ok_or_else(|| SchemaError::Malformed(name.to_string()))?;
        for key in object.keys() {
            if !PROPERTY_KEYWORDS.contains(&key.as_str()) {
                return Err(SchemaError::UnsupportedKeyword(format!("{name}.{key}")));
            }
        }
        let mut types = BTreeSet::new();
        match object.get("type") {
            Some(Value::String(single)) => {
                types.insert(JsonType::parse(single).ok_or_else(|| SchemaError::UnsupportedType(single.clone()))?);
            }
            Some(Value::Array(many)) => {
                for item in many {
                    let item = item.as_str().ok_or_else(|| SchemaError::Malformed(name.to_string()))?;
                    types.insert(JsonType::parse(item).ok_or_else(|| SchemaError::UnsupportedType(item.to_string()))?);
                }
            }
            // A property with no type would accept anything: refused.
            _ => return Err(SchemaError::UnsupportedType(format!("{name}: missing type"))),
        }
        if object.get("nullable").and_then(Value::as_bool) == Some(true) {
            types.insert(JsonType::Null);
        }
        if let Some(format) = object.get("format").and_then(Value::as_str)
            && !FORMATS.contains(&format)
        {
            return Err(SchemaError::UnsupportedKeyword(format!("{name}.format={format}")));
        }
        let usize_of = |key: &str| object.get(key).and_then(Value::as_u64).map(|n| n as usize);
        let enumeration = match object.get("enum") {
            None => None,
            Some(Value::Array(values)) => Some(values.clone()),
            Some(_) => return Err(SchemaError::Malformed(format!("{name}.enum"))),
        };
        // Integer formats imply their range.
        let (format_min, format_max) = match object.get("format").and_then(Value::as_str) {
            Some("int32") => (Some(f64::from(i32::MIN)), Some(f64::from(i32::MAX))),
            Some("uint32") => (Some(0.0), Some(f64::from(u32::MAX))),
            Some("uint" | "uint8" | "uint16" | "uint64") => (Some(0.0), None),
            _ => (None, None),
        };
        Ok(Self {
            types,
            enumeration,
            min_length: usize_of("minLength"),
            max_length: usize_of("maxLength").unwrap_or(DEFAULT_MAX_STRING_CHARS),
            minimum: object.get("minimum").and_then(Value::as_f64).or(format_min),
            maximum: object.get("maximum").and_then(Value::as_f64).or(format_max),
        })
    }

    fn check(&self, name: &str, value: &Value) -> Result<(), ArgumentError> {
        let matches_type = self.types.iter().any(|ty| match ty {
            JsonType::String => value.is_string(),
            // An integer is a JSON number written without a fraction or
            // exponent. `5.0` and `5e0` are refused, exactly as the MCP
            // server's `Option<i64>` field refuses them.
            JsonType::Integer => value.is_i64() || value.is_u64(),
            JsonType::Number => value.as_f64().is_some_and(f64::is_finite),
            JsonType::Boolean => value.is_boolean(),
            JsonType::Null => value.is_null(),
        });
        if !matches_type {
            let expected = self.types.iter().map(|ty| ty.name()).collect::<Vec<_>>().join(" or ");
            return Err(ArgumentError::WrongType { name: name.to_string(), expected });
        }
        if value.is_null() {
            return Ok(());
        }
        if let Some(allowed) = &self.enumeration
            && !allowed.contains(value)
        {
            return Err(ArgumentError::NotInEnum(name.to_string()));
        }
        if let Some(text) = value.as_str() {
            let chars = text.chars().count();
            let min = self.min_length.unwrap_or(0);
            if chars < min || chars > self.max_length {
                return Err(ArgumentError::Length { name: name.to_string(), min, max: self.max_length });
            }
        }
        if let Some(number) = value.as_f64()
            && (self.minimum.is_some_and(|min| number < min) || self.maximum.is_some_and(|max| number > max))
        {
            return Err(ArgumentError::Range(name.to_string()));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    /// What schemars 1.x derives for the MCP server's `SearchTicketsArgs`.
    fn search_tickets() -> ObjectSchema {
        ObjectSchema::parse(&json!({
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "SearchTicketsArgs",
            "type": "object",
            "properties": {
                "status": {"description": "Optional ticket status", "type": ["string", "null"]},
                "limit": {"description": "Maximum number", "type": ["integer", "null"], "format": "int64"}
            }
        }))
        .unwrap()
    }

    fn get_ticket() -> ObjectSchema {
        ObjectSchema::parse(&json!({
            "type": "object",
            "properties": {"ticket_id": {"type": "string", "description": "Exact id"}},
            "required": ["ticket_id"]
        }))
        .unwrap()
    }

    #[test]
    fn well_formed_arguments_pass_unchanged() {
        let args = json!({"status": "open", "limit": 5});
        assert_eq!(Value::Object(search_tickets().validate(&args).unwrap()), args);
        assert!(search_tickets().validate(&json!({})).is_ok());
        assert!(search_tickets().validate(&json!({"status": null})).is_ok());
    }

    #[test]
    fn unknown_arguments_are_refused_even_without_additional_properties_false() {
        assert_eq!(
            search_tickets().validate(&json!({"status": "open", "user_id": "admin"})),
            Err(ArgumentError::Unknown("user_id".into()))
        );
        assert_eq!(
            get_ticket().validate(&json!({"ticket_id": "TKT-1", "approved": true})),
            Err(ArgumentError::Unknown("approved".into()))
        );
    }

    #[test]
    fn numbers_are_not_coerced() {
        for bad in [json!({"limit": 5.0}), json!({"limit": 5.5}), json!({"limit": "5"}), json!({"limit": 1e3})] {
            assert!(matches!(search_tickets().validate(&bad), Err(ArgumentError::WrongType { .. })), "{bad}");
        }
        assert!(search_tickets().validate(&json!({"limit": -3})).is_ok(), "range is the server's to clamp");
    }

    #[test]
    fn required_and_type_checks() {
        assert_eq!(get_ticket().validate(&json!({})), Err(ArgumentError::Missing("ticket_id".into())));
        assert_eq!(get_ticket().validate(&json!({"ticket_id": null})), Err(ArgumentError::Missing("ticket_id".into())));
        assert!(matches!(get_ticket().validate(&json!({"ticket_id": 1001})), Err(ArgumentError::WrongType { .. })));
        assert_eq!(get_ticket().validate(&json!(["TKT-1001"])), Err(ArgumentError::NotAnObject));
        let long = "x".repeat(DEFAULT_MAX_STRING_CHARS + 1);
        assert!(matches!(get_ticket().validate(&json!({"ticket_id": long})), Err(ArgumentError::Length { .. })));
    }

    #[test]
    fn unsupported_schemas_are_refused_at_discovery() {
        for schema in [
            json!({"type": "object", "oneOf": []}),
            json!({"type": "object", "properties": {"a": {"$ref": "#/defs/x"}}}),
            json!({"type": "object", "properties": {"a": {"type": "string", "pattern": "x"}}}),
            json!({"type": "object", "properties": {"a": {}}}),
            json!({"type": "object", "properties": {"a": {"type": "array"}}}),
            json!({"type": "object", "additionalProperties": {"type": "string"}}),
            json!({"type": "array"}),
        ] {
            assert!(ObjectSchema::parse(&schema).is_err(), "{schema}");
        }
    }

    #[test]
    fn a_tool_that_accepts_identity_or_approval_fields_is_not_exposed() {
        for reserved in ["user_id", "actor_id", "approval_token", "Approved", "request_id"] {
            let schema = json!({"type": "object", "properties": {reserved: {"type": "string"}}});
            assert_eq!(ObjectSchema::parse(&schema), Err(SchemaError::ReservedArgument(reserved.to_string())));
        }
    }

    #[test]
    fn enums_lengths_and_ranges_are_enforced() {
        let schema = ObjectSchema::parse(&json!({
            "type": "object",
            "properties": {
                "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
                "summary": {"type": "string", "minLength": 1, "maxLength": 10},
                "count": {"type": "integer", "minimum": 1, "maximum": 3}
            }
        }))
        .unwrap();
        assert!(schema.validate(&json!({"priority": "high"})).is_ok());
        assert_eq!(schema.validate(&json!({"priority": "critical"})), Err(ArgumentError::NotInEnum("priority".into())));
        assert!(matches!(schema.validate(&json!({"summary": ""})), Err(ArgumentError::Length { .. })));
        assert_eq!(schema.validate(&json!({"count": 4})), Err(ArgumentError::Range("count".into())));
    }
}
