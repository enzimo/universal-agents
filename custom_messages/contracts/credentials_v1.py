"""credentials v1 contracts, independent of runtime hosts and SDKs."""

from custom_messages.enums import MessageMetaType
from id_generation import new_id
from pydantic import BaseModel
from pydantic import Field
from pydantic import model_validator
from typing import Any
from typing import Dict
from typing import List
from typing import Literal
from typing import Optional


SecureCredentialResultStatus = Literal[
    "signup_completed",
    "manual_signup_required",
    "manual_signup_collected",
    "signup_failed",
    "credential_stored",
    "credential_deleted",
    "credential_unavailable",
]


class CredentialFormFieldSpec(BaseModel):
    """User-facing broker form hint for a credential field."""

    input_control: Literal["text", "password", "textarea", "file"] = Field(
        default="text"
    )
    autocomplete: Optional[str] = Field(default=None)
    accept: List[str] = Field(default_factory=list)
    help_text: Optional[str] = Field(default=None)


class CredentialTemplateReference(BaseModel):
    """Non-secret reference to a credential template used to build a request."""

    template_id: str
    template_source: Literal["builtin", "approved_variant", "agent_generated"] = (
        "builtin"
    )
    base_template_id: Optional[str] = None
    template_version: int = 1


class CredentialTemplateVariant(BaseModel):
    """Agent-generated or augmented non-secret template candidate."""

    template_id: Optional[str] = None
    base_template_id: Optional[str] = None
    template: Dict[str, Any] = Field(default_factory=dict)
    augmentation_summary: Optional[str] = None


class SecureCredentialFieldSpec(BaseModel):
    """A non-secret field contract for broker-owned credential collection."""

    name: str = Field(
        description="Catalog field name, such as username or service_password."
    )
    kind: str = Field(
        default="custom",
        description="Field kind: username, email, password, token, api_key, or custom.",
    )
    visibility: str = Field(default="secret", description="metadata_visible or secret.")
    value_source: str = Field(
        default="user_input",
        description="user_input, agent_supplied_metadata, user_or_agent_supplied_metadata, or store_generated_secret.",
    )
    required: bool = Field(default=True, description="Whether this field is required.")
    value: Optional[str] = Field(
        default=None,
        description="Metadata-visible value only. Do not put secrets in this field.",
    )
    generation_policy: Optional[str] = Field(
        default=None,
        description="Generation policy for generated secret fields.",
    )
    label: Optional[str] = Field(
        default=None, description="Optional user-facing field label for broker forms."
    )
    description: Optional[str] = Field(
        default=None, description="Optional non-secret field description."
    )
    placeholder: Optional[str] = Field(
        default=None, description="Optional non-secret placeholder text."
    )
    form: Optional[CredentialFormFieldSpec] = Field(
        default=None, description="Optional broker form rendering hints."
    )


class SecureCredentialCollectionRequest(BaseModel):
    """Request for the gateway/broker to collect or generate named credentials."""

    message_class: Literal["SecureCredentialCollectionRequest"] = Field(
        default="SecureCredentialCollectionRequest",
        description="Secure Credential Collection Request",
    )
    credential_request_id: str = Field(
        default_factory=lambda: new_id("cred_req"),
        description="Unique id for this credential request.",
    )
    request_type: MessageMetaType = Field(default=MessageMetaType.REQUEST)
    parent_task_id: Optional[str] = Field(
        default=None, description="Task waiting on this credential."
    )
    service_name: str = Field(description="Service or site this credential is for.")
    signup_url: Optional[str] = Field(
        default=None, description="Signup or account URL/domain."
    )
    credential_key: str = Field(description="Stable catalog key chosen by the agent.")
    display_name: Optional[str] = Field(
        default=None, description="Human-readable catalog display name."
    )
    purpose: str = Field(
        description="Why the credential is needed in future workflows."
    )
    store_target: str = Field(
        default="default",
        description="Configured credential store target, or an explicit source name.",
    )
    flow: str = Field(
        default="credential_collection",
        description="credential_collection, signup, manual_signup, or credential_deletion.",
    )
    source: Optional[str] = Field(
        default=None,
        description="Credential source for credential_deletion requests when deleting a live source item directly.",
    )
    source_item_id: Optional[str] = Field(
        default=None,
        description="Source item id for credential_deletion requests, such as a Sealbox credential key.",
    )
    source_item_name: Optional[str] = Field(
        default=None,
        description="Source item name for credential_deletion requests when no source item id is available.",
    )
    fields: List[SecureCredentialFieldSpec] = Field(default_factory=list)
    template_reference: Optional[CredentialTemplateReference] = Field(
        default=None,
        description="Non-secret template reference used to render this request.",
    )
    template_variant: Optional[CredentialTemplateVariant] = Field(
        default=None,
        description="Non-secret approved-template candidate to persist after user approval.",
    )
    save_template_variant_on_approval: bool = Field(
        default=False,
        description="Whether the broker should save the non-secret template variant after user approval.",
    )
    automation_steps: List[str] = Field(
        default_factory=list,
        description="Approved signup automation steps. These must not contain secrets.",
    )
    user_approved: bool = Field(
        default=False,
        description="Whether the user approved this collection/signup attempt.",
    )
    continue_task: bool = Field(
        default=False,
        description=(
            "Set true when the task has more to do after the form is used, for example link the "
            "stored credential to a tool and then use it; the task then resumes from the result. "
            "Leave false when storing or deleting the credential is the whole request; the runtime "
            "then answers the person itself."
        ),
    )

    def get_id(self) -> str:
        return self.credential_request_id


class SecureCredentialCollectionResult(BaseModel):
    """Redacted completion or fallback metadata for secure credential collection."""

    message_class: Literal["SecureCredentialCollectionResult"] = Field(
        default="SecureCredentialCollectionResult",
        description="Secure Credential Collection Result",
    )
    credential_request_id: str = Field(description="Request id this result answers.")
    result_type: MessageMetaType = Field(default=MessageMetaType.RESPONSE)
    status: SecureCredentialResultStatus = Field(
        description="Credential or signup flow status."
    )
    credential_key: str = Field(description="Catalog key associated with this result.")
    service_name: str = Field(
        description="Human-readable service name copied from the original request context."
    )
    display_name: Optional[str] = Field(
        default=None,
        description="Optional human-readable credential name copied from the original request context.",
    )
    parent_task_id: Optional[str] = Field(
        default=None, description="Task waiting on this result."
    )
    broker_session_id: Optional[str] = Field(
        default=None, description="Broker form/session id, never a credential."
    )
    manual_fallback_url: Optional[str] = Field(
        default=None,
        description="Secure broker form URL when manual signup is required.",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Redacted credential metadata only."
    )
    message: str = Field(
        default="",
        description="Optional redacted broker detail for the waiting agent, not final user-facing copy.",
    )
    error: Optional[str] = Field(
        default=None, description="Redacted error text, when any."
    )
    continue_task: bool = Field(
        default=False,
        description="Copied from the request: the task resumes from this result instead of ending.",
    )

    def get_id(self) -> str:
        return self.credential_request_id


# What a tool needs before an agent can use it for a person
# (``ToolSetupRequestV1``): the person's own sign-in (GitHub, Google, X), an
# account link that says which stored credential the tool uses (the person's
# own or the team's), or the team's sign-in app that people sign in through
# (the Google OAuth client, the X app; administrators only), or a step done
# outside Universal Agents that the tool's catalog entry describes and the
# person marks done (``external_step``, ``access_contract ToolSetupSpecV1``).
ToolSetupKind = Literal["sign_in", "account_link", "team_sign_in_app", "external_step"]
TOOL_SETUP_SIGN_IN: ToolSetupKind = "sign_in"
TOOL_SETUP_ACCOUNT_LINK: ToolSetupKind = "account_link"
TOOL_SETUP_TEAM_SIGN_IN_APP: ToolSetupKind = "team_sign_in_app"
TOOL_SETUP_EXTERNAL_STEP: ToolSetupKind = "external_step"
# Whose account or app the setup is for.
ToolSetupOwner = Literal["team", "person"]
TOOL_SETUP_OWNER_PERSON: ToolSetupOwner = "person"
TOOL_SETUP_OWNER_TEAM: ToolSetupOwner = "team"
# How the setup page ended: finished, closed without finishing, timed out, or
# could not be opened or confirmed.
ToolSetupResultStatus = Literal["completed", "declined", "expired", "unavailable"]
TOOL_SETUP_COMPLETED: ToolSetupResultStatus = "completed"
TOOL_SETUP_DECLINED: ToolSetupResultStatus = "declined"
TOOL_SETUP_EXPIRED: ToolSetupResultStatus = "expired"
TOOL_SETUP_UNAVAILABLE: ToolSetupResultStatus = "unavailable"
# Where the result came from: the page the task asked for, or a page the
# person opened themselves (``/link``, ``/auth ... team_client``) while the
# task waited.
ToolSetupSource = Literal["task_request", "person_action"]
TOOL_SETUP_SOURCE_TASK: ToolSetupSource = "task_request"
TOOL_SETUP_SOURCE_PERSON: ToolSetupSource = "person_action"
# How many setup pages of one kind one task may offer for one tool (owner's
# decision 2026-09-28): a page that expired may be offered again, and the
# gateway refuses the next request after this many.
TOOL_SETUP_OFFER_LIMIT = 3
TOOL_SETUP_REASON_MAX_CHARS = 2000


class ToolSetupRequestV1(BaseModel):
    """Trusted code asks the person to set up one tool so TeamLead's task can use it.

    Sent only by ``set_up_tools`` (``custom_agents.tool_setup_run_runtime``),
    never written by a model. The gateway opens the matching secure broker
    page in the chat of the person the task works for: their own sign-in page
    (``sign_in``), the labeled account link form (``account_link``;
    ``owner="team"`` needs a team administrator), or the page that stores the
    team's sign-in app (``team_sign_in_app``; administrators only). For an
    ``external_step`` it shows the catalog's words for that step (named by
    ``step_id``; ``owner="team"`` when only an administrator can do it) and
    waits until the person marks it done on the workflow page. The task waits
    for one ``ToolSetupResultV1``. Setting a tool up never grants access to
    it: access comes from grants.
    """

    message_class: Literal["ToolSetupRequestV1"] = Field(default="ToolSetupRequestV1")
    setup_request_id: str = Field(default_factory=lambda: new_id("tool_setup_req"))
    request_type: MessageMetaType = Field(default=MessageMetaType.REQUEST)
    parent_task_id: str = Field(description="Task waiting for the setup result.")
    tool_id: str = Field(description="Catalog tool id, for example cli:github or http:portainer.")
    kind: ToolSetupKind
    owner: ToolSetupOwner = Field(description="Whose account or app: the team's or the person's own.")
    reason: str = Field(
        max_length=TOOL_SETUP_REASON_MAX_CHARS,
        description="One plain sentence the person reads: why the task needs this.",
    )
    step_id: Optional[str] = Field(
        default=None,
        max_length=40,
        description="For an external_step: the step id in the tool's catalog setup data.",
    )

    @model_validator(mode="after")
    def _owner_fits_kind(self) -> "ToolSetupRequestV1":
        if self.kind == TOOL_SETUP_SIGN_IN and self.owner != TOOL_SETUP_OWNER_PERSON:
            raise ValueError("A sign-in is always the person's own (owner person).")
        if self.kind == TOOL_SETUP_TEAM_SIGN_IN_APP and self.owner != TOOL_SETUP_OWNER_TEAM:
            raise ValueError("The team's sign-in app belongs to the team (owner team).")
        if (self.kind == TOOL_SETUP_EXTERNAL_STEP) != bool(self.step_id):
            raise ValueError("An external step names its step_id, and only an external step does.")
        return self

    def get_id(self) -> str:
        return self.setup_request_id


class ToolSetupResultV1(BaseModel):
    """How one setup page ended, sent to the task waiting for it; no secrets.

    ``message`` is a plain sentence TeamLead may relay; ``account_label`` is
    the account the person signed in with when the broker knows it (for
    example ``exampleuser``). ``needs_administrator`` says only a team
    administrator can do this step. ``attempt`` counts the pages of this kind
    the task offered for the tool (at most ``TOOL_SETUP_OFFER_LIMIT``).
    """

    message_class: Literal["ToolSetupResultV1"] = Field(default="ToolSetupResultV1")
    setup_request_id: str
    result_type: MessageMetaType = Field(default=MessageMetaType.RESPONSE)
    status: ToolSetupResultStatus
    parent_task_id: str
    tool_id: str
    kind: ToolSetupKind
    owner: ToolSetupOwner
    source: ToolSetupSource = TOOL_SETUP_SOURCE_TASK
    attempt: int = Field(default=1, ge=0)
    account_label: Optional[str] = Field(default=None, max_length=200)
    needs_administrator: bool = False
    reason_code: Optional[str] = None
    message: str = ""
    link_id: Optional[str] = None
    broker_session_id: Optional[str] = None
    step_id: Optional[str] = Field(default=None, max_length=40)

    def get_id(self) -> str:
        return self.setup_request_id


class SignupAutomationRequest(BaseModel):
    """Request to attempt an approved signup without exposing secrets to agents."""

    message_class: Literal["SignupAutomationRequest"] = Field(
        default="SignupAutomationRequest",
        description="Signup Automation Request",
    )
    signup_request_id: str = Field(default_factory=lambda: new_id("signup_req"))
    request_type: MessageMetaType = Field(default=MessageMetaType.REQUEST)
    parent_task_id: Optional[str] = None
    service_name: str
    signup_url: str
    desired_username: Optional[str] = None
    credential_key: str
    password_field_name: str = "password"
    store_target: str = "default"
    automation_steps: List[str] = Field(default_factory=list)
    user_approved: bool = False

    def get_id(self) -> str:
        return self.signup_request_id


class SignupAutomationResult(BaseModel):
    """Redacted result of a signup attempt or manual-signup boundary."""

    message_class: Literal["SignupAutomationResult"] = Field(
        default="SignupAutomationResult",
        description="Signup Automation Result",
    )
    signup_request_id: str
    result_type: MessageMetaType = Field(default=MessageMetaType.RESPONSE)
    status: SecureCredentialResultStatus
    credential_key: str
    parent_task_id: Optional[str] = None
    boundary_reason: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    message: str = ""
    error: Optional[str] = None

    def get_id(self) -> str:
        return self.signup_request_id
