//! Access model (version 2) evaluator.
//!
//! The Python decision service (`access_control`) materializes, for one
//! request, the grants that are valid now into template links and builds the
//! principal and target entities; this evaluator links them into the static
//! policy set, validates the linked set strictly, and decides. It never reads a
//! grant store itself, so validity, lifetimes and use counts stay in one place.
//!
//! Any evaluation error denies: Cedar skips a policy whose evaluation errors,
//! which could otherwise silently drop a `forbid`.

use cedar_policy::{
    Authorizer, Context, Decision, Entities, EntityUid, PolicyId, PolicySet, Request, Schema,
    SlotId, ValidationMode, Validator,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::path::Path;
use std::str::FromStr;

pub const ACCESS_PROTOCOL_VERSION: u32 = 3;

#[derive(Debug, Clone, Deserialize)]
pub struct EntityRefV2 {
    #[serde(rename = "type")]
    pub kind: String,
    pub id: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct AccessLinkV2 {
    pub template: String,
    pub id: String,
    pub principal: Option<EntityRefV2>,
    pub resource: Option<EntityRefV2>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct AccessRequestV2 {
    pub request_id: String,
    #[serde(default)]
    pub links: Vec<AccessLinkV2>,
    pub entities: Value,
    pub principal: EntityRefV2,
    pub action: String,
    pub resource: EntityRefV2,
    #[serde(default)]
    pub context: Option<Value>,
    #[serde(default)]
    pub extra_policies: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct AccessDecisionV2 {
    pub protocol_version: u32,
    pub message_type: String,
    pub request_id: String,
    pub ok: bool,
    pub allowed: bool,
    pub determining_policy_ids: Vec<String>,
    pub errors: Vec<String>,
    pub error_code: Option<String>,
    pub error_message: Option<String>,
    pub policy_digest: String,
}

impl AccessDecisionV2 {
    fn failed(request_id: &str, policy_digest: &str, code: &str, message: String) -> Self {
        Self {
            protocol_version: ACCESS_PROTOCOL_VERSION,
            message_type: "access_decision".to_owned(),
            request_id: request_id.to_owned(),
            ok: false,
            allowed: false,
            determining_policy_ids: Vec::new(),
            errors: Vec::new(),
            error_code: Some(code.to_owned()),
            error_message: Some(message),
            policy_digest: policy_digest.to_owned(),
        }
    }
}

fn uid(entity: &EntityRefV2) -> Result<EntityUid, String> {
    let escaped = serde_json::to_string(&entity.id).map_err(|e| e.to_string())?;
    EntityUid::from_str(&format!("{}::{escaped}", entity.kind)).map_err(|e| e.to_string())
}

/// Cedar's text parser assigns positional ids; policies are addressed by @id.
pub fn with_annotation_ids(source: &str) -> Result<PolicySet, String> {
    let parsed = PolicySet::from_str(source).map_err(|e| format!("{e:?}"))?;
    let mut set = PolicySet::new();
    for template in parsed.templates() {
        let id = template.annotation("id").ok_or("template without @id")?;
        set.add_template(template.new_id(PolicyId::from_str(id).map_err(|e| e.to_string())?))
            .map_err(|e| format!("{e:?}"))?;
    }
    for policy in parsed.policies() {
        let id = policy.annotation("id").ok_or("policy without @id")?;
        set.add(policy.new_id(PolicyId::from_str(id).map_err(|e| e.to_string())?))
            .map_err(|e| format!("{e:?}"))?;
    }
    Ok(set)
}

pub struct AccessEvaluator {
    schema: Schema,
    base: PolicySet,
    policy_digest: String,
}

impl AccessEvaluator {
    pub fn load(policy_path: &Path, schema_path: &Path) -> Result<Self, String> {
        let policy_source = std::fs::read_to_string(policy_path)
            .map_err(|e| format!("cannot read {}: {e}", policy_path.display()))?;
        let schema_source = std::fs::read_to_string(schema_path)
            .map_err(|e| format!("cannot read {}: {e}", schema_path.display()))?;
        let (schema, _warnings) =
            Schema::from_cedarschema_str(&schema_source).map_err(|e| format!("schema: {e:?}"))?;
        let base = with_annotation_ids(&policy_source)?;
        let validation = Validator::new(schema.clone()).validate(&base, ValidationMode::Strict);
        if !validation.validation_passed() {
            let errors: Vec<String> = validation
                .validation_errors()
                .map(|e| e.to_string())
                .collect();
            return Err(format!("policy validation failed: {}", errors.join("; ")));
        }
        let mut hasher = Sha256::new();
        hasher.update(schema_source.as_bytes());
        hasher.update(b"\0");
        hasher.update(policy_source.as_bytes());
        let policy_digest = format!("{:x}", hasher.finalize());
        Ok(Self {
            schema,
            base,
            policy_digest,
        })
    }

    pub fn ready(&self) -> Value {
        json!({
            "protocol_version": ACCESS_PROTOCOL_VERSION,
            "message_type": "access_ready",
            "policy_digest": self.policy_digest,
        })
    }

    fn linked_policies(&self, request: &AccessRequestV2) -> Result<PolicySet, String> {
        let mut policies = self.base.clone();
        if let Some(extra) = request.extra_policies.as_deref() {
            // Approved restrictions compiled from the rule vocabulary: forbid only.
            let extra_set =
                with_annotation_ids(extra).map_err(|e| format!("extra policies: {e}"))?;
            for policy in extra_set.policies() {
                if policy.effect() != cedar_policy::Effect::Forbid {
                    return Err("extra policies may only forbid".to_owned());
                }
                policies
                    .add(policy.clone())
                    .map_err(|e| format!("extra policy: {e}"))?;
            }
        }
        for link in &request.links {
            let template = PolicyId::from_str(&link.template).map_err(|e| e.to_string())?;
            let new_id = PolicyId::from_str(&link.id).map_err(|e| e.to_string())?;
            let mut slots = HashMap::new();
            if let Some(principal) = &link.principal {
                slots.insert(SlotId::principal(), uid(principal)?);
            }
            if let Some(resource) = &link.resource {
                slots.insert(SlotId::resource(), uid(resource)?);
            }
            policies
                .link(template, new_id, slots)
                .map_err(|e| format!("link {}: {e:?}", link.id))?;
        }
        let validation =
            Validator::new(self.schema.clone()).validate(&policies, ValidationMode::Strict);
        if !validation.validation_passed() {
            let errors: Vec<String> = validation
                .validation_errors()
                .map(|e| e.to_string())
                .collect();
            return Err(format!(
                "linked policy validation failed: {}",
                errors.join("; ")
            ));
        }
        Ok(policies)
    }

    pub fn decide(&self, request: &AccessRequestV2) -> AccessDecisionV2 {
        match self.try_decide(request) {
            Ok(decision) => decision,
            Err(message) => AccessDecisionV2::failed(
                &request.request_id,
                &self.policy_digest,
                "access_evaluation_failed",
                message,
            ),
        }
    }

    fn try_decide(&self, request: &AccessRequestV2) -> Result<AccessDecisionV2, String> {
        let policies = self.linked_policies(request)?;
        let entities = Entities::from_json_value(request.entities.clone(), Some(&self.schema))
            .map_err(|e| format!("entities: {e}"))?;
        let action = EntityUid::from_str(&format!(
            "Action::{}",
            serde_json::to_string(&request.action).map_err(|e| e.to_string())?
        ))
        .map_err(|e| e.to_string())?;
        let context = Context::from_json_value(
            request.context.clone().unwrap_or_else(|| json!({})),
            Some((&self.schema, &action)),
        )
        .map_err(|e| format!("context: {e}"))?;
        let cedar_request = Request::new(
            uid(&request.principal)?,
            action,
            uid(&request.resource)?,
            context,
            Some(&self.schema),
        )
        .map_err(|e| format!("request: {e}"))?;
        let response = Authorizer::new().is_authorized(&cedar_request, &policies, &entities);
        let mut determining: Vec<String> = response
            .diagnostics()
            .reason()
            .map(|p| p.to_string())
            .collect();
        determining.sort();
        let errors: Vec<String> = response
            .diagnostics()
            .errors()
            .map(|e| e.to_string())
            .collect();
        // An evaluation error can silently skip a forbid, so it always denies.
        let allowed = response.decision() == Decision::Allow && errors.is_empty();
        Ok(AccessDecisionV2 {
            protocol_version: ACCESS_PROTOCOL_VERSION,
            message_type: "access_decision".to_owned(),
            request_id: request.request_id.clone(),
            ok: errors.is_empty(),
            allowed,
            determining_policy_ids: determining,
            errors,
            error_code: None,
            error_message: None,
            policy_digest: self.policy_digest.clone(),
        })
    }
}

/// Decide one serialized request line; malformed input is a denial.
pub fn decide_line(evaluator: &AccessEvaluator, line: &str) -> AccessDecisionV2 {
    match serde_json::from_str::<AccessRequestV2>(line) {
        Ok(request) => evaluator.decide(&request),
        Err(error) => {
            let request_id = serde_json::from_str::<Value>(line)
                .ok()
                .and_then(|value| {
                    value
                        .get("request_id")
                        .and_then(Value::as_str)
                        .map(ToOwned::to_owned)
                })
                .unwrap_or_else(|| "unknown".to_owned());
            AccessDecisionV2::failed(
                &request_id,
                &evaluator.policy_digest,
                "protocol_request_invalid",
                error.to_string(),
            )
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    const SCHEMA: &str = r#"
        entity User;
        entity Tool;
        action "level.read" appliesTo { principal: [User], resource: [Tool] };
    "#;
    const POLICIES: &str = r#"
        @id("tpl:grant")
        permit (principal == ?principal, action == Action::"level.read", resource == ?resource);
    "#;

    /// Write the sources to a per-test folder and load them like the binary does.
    fn evaluator(name: &str, policies: &str, schema: &str) -> Result<AccessEvaluator, String> {
        let dir = std::env::temp_dir().join(format!(
            "ua-cedar-authorizer-test-{}-{name}",
            std::process::id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let policy_path = dir.join("policies.cedar");
        let schema_path = dir.join("schema.cedarschema");
        std::fs::write(&policy_path, policies).unwrap();
        std::fs::write(&schema_path, schema).unwrap();
        let loaded = AccessEvaluator::load(&policy_path, &schema_path);
        std::fs::remove_dir_all(&dir).unwrap();
        loaded
    }

    fn request_line(links: Value, extra_policies: Option<&str>) -> String {
        json!({
            "request_id": "req-1",
            "links": links,
            "entities": [
                {"uid": {"type": "User", "id": "alice"}, "attrs": {}, "parents": []},
                {"uid": {"type": "Tool", "id": "gh"}, "attrs": {}, "parents": []}
            ],
            "principal": {"type": "User", "id": "alice"},
            "action": "level.read",
            "resource": {"type": "Tool", "id": "gh"},
            "extra_policies": extra_policies,
        })
        .to_string()
    }

    fn grant_link() -> Value {
        json!([{
            "template": "tpl:grant",
            "id": "grant:1",
            "principal": {"type": "User", "id": "alice"},
            "resource": {"type": "Tool", "id": "gh"}
        }])
    }

    #[test]
    fn shipped_access_policies_validate_against_the_shipped_schema() {
        let root =
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../ua_definitions/access/cedar");
        let evaluator = AccessEvaluator::load(
            &root.join("policies.cedar"),
            &root.join("schema.cedarschema"),
        )
        .expect("the shipped access policies must validate strictly");
        let ready = evaluator.ready();
        assert_eq!(ready["message_type"], "access_ready");
        assert_eq!(ready["protocol_version"], ACCESS_PROTOCOL_VERSION);
        assert_eq!(ready["policy_digest"].as_str().unwrap().len(), 64);
    }

    #[test]
    fn a_linked_grant_allows_and_its_absence_denies() {
        let evaluator = evaluator("grant", POLICIES, SCHEMA).unwrap();
        let allowed = decide_line(&evaluator, &request_line(grant_link(), None));
        assert!(allowed.ok && allowed.allowed, "{allowed:?}");
        assert_eq!(allowed.determining_policy_ids, vec!["grant:1".to_owned()]);
        assert_eq!(allowed.request_id, "req-1");

        let denied = decide_line(&evaluator, &request_line(json!([]), None));
        assert!(denied.ok && !denied.allowed, "{denied:?}");
        assert!(denied.determining_policy_ids.is_empty());
    }

    #[test]
    fn extra_policies_may_only_forbid() {
        let evaluator = evaluator("extra", POLICIES, SCHEMA).unwrap();
        let block = r#"@id("block:1") forbid (principal, action, resource);"#;
        let blocked = decide_line(&evaluator, &request_line(grant_link(), Some(block)));
        assert!(blocked.ok && !blocked.allowed, "{blocked:?}");
        assert_eq!(blocked.determining_policy_ids, vec!["block:1".to_owned()]);

        let widen = r#"@id("extra:1") permit (principal, action, resource);"#;
        let refused = decide_line(&evaluator, &request_line(json!([]), Some(widen)));
        assert!(!refused.ok && !refused.allowed, "{refused:?}");
        assert_eq!(
            refused.error_code.as_deref(),
            Some("access_evaluation_failed")
        );
    }

    #[test]
    fn malformed_requests_are_denials_that_keep_the_request_id() {
        let evaluator = evaluator("malformed", POLICIES, SCHEMA).unwrap();
        let decision = decide_line(&evaluator, r#"{"request_id": "req-9", "links": "nope"}"#);
        assert!(!decision.ok && !decision.allowed);
        assert_eq!(decision.request_id, "req-9");
        assert_eq!(
            decision.error_code.as_deref(),
            Some("protocol_request_invalid")
        );

        let unreadable = decide_line(&evaluator, "not json");
        assert_eq!(unreadable.request_id, "unknown");
        assert_eq!(
            unreadable.error_code.as_deref(),
            Some("protocol_request_invalid")
        );
    }

    #[test]
    fn policies_that_fail_strict_validation_are_refused_at_load() {
        let unknown_action =
            r#"@id("bad") permit (principal, action == Action::"level.write", resource);"#;
        let error = evaluator("invalid", unknown_action, SCHEMA)
            .err()
            .expect("must refuse");
        assert!(
            error.contains("policy validation failed") || error.contains("level.write"),
            "{error}"
        );

        let missing_id = r#"permit (principal, action, resource);"#;
        let error = evaluator("no-id", missing_id, SCHEMA)
            .err()
            .expect("must refuse");
        assert!(error.contains("without @id"), "{error}");
    }
}
