from __future__ import annotations

from pydantic import Field

from app.api.schemas import StrictRequest


class KnowledgeSearchRequest(StrictRequest):
    query: str = Field(min_length=1, max_length=500)
    scope_type: str = Field(pattern=r"^(platform|store)$")
    scope_id: str = Field(min_length=1, max_length=64)
    limit: int = Field(default=5, ge=1, le=20)


class KnowledgeCitation(StrictRequest):
    document_id: str
    content_version: str
    title: str
    excerpt: str
    score: float


class KnowledgeSearchResult(StrictRequest):
    items: list[KnowledgeCitation]
    degraded: bool = False


class KnowledgeDocumentCreate(StrictRequest):
    scope_type: str = Field(pattern=r"^(platform|store)$")
    scope_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    safe_text: str = Field(min_length=1, max_length=200000)


class KnowledgeDocumentView(StrictRequest):
    document_id: str
    scope_type: str
    scope_id: str
    title: str
    content_version: str
    status: str
    index_job_no: str | None = None
    index_status: str | None = None


class KnowledgeDocumentList(StrictRequest):
    items: list[KnowledgeDocumentView]


class KnowledgeIndexJobView(StrictRequest):
    job_id: str
    command_job_id: str
    status: str
    progress: int
    error_code: str | None = None


class SkillDefinitionCreate(StrictRequest):
    skill_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    display_name: str = Field(min_length=1, max_length=128)


class SkillVersionCreate(StrictRequest):
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    instructions: str = Field(min_length=1, max_length=20000)
    evaluation_report: dict[str, object] = Field(default_factory=dict)


class SkillView(StrictRequest):
    skill_id: str
    skill_code: str
    display_name: str
    status: str
    latest_version: int | None
    published_version: int | None


class SkillList(StrictRequest):
    items: list[SkillView]


class ToolCreate(StrictRequest):
    tool_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    server_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")
    risk_level: str = Field(pattern=r"^(read|low|medium|high)$")


class ToolVersionCreate(StrictRequest):
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    evaluation_report: dict[str, object]


class ToolView(ToolCreate):
    input_schema: dict[str, object] | None = None
    output_schema: dict[str, object] | None = None
    status: str
    latest_version: int | None = None
    published_version: int | None = None


class ToolList(StrictRequest):
    items: list[ToolView]


class AgentVersionSummary(StrictRequest):
    version_no: int
    status: str
    model_profile: str
    tool_allowlist: list[str]
    system_prompt: str
    policy_config: dict[str, object]


class AgentVersionCreate(StrictRequest):
    system_prompt: str = Field(min_length=1, max_length=50000)
    model_profile: str = Field(min_length=1, max_length=64)
    tool_allowlist: list[str] = Field(max_length=100)
    policy_config: dict[str, object]


class AgentView(StrictRequest):
    agent_id: str
    agent_code: str
    display_name: str
    scope_type: str
    status: str
    versions: list[AgentVersionSummary]


class AgentList(StrictRequest):
    items: list[AgentView]


class KillSwitchChange(StrictRequest):
    reason: str = Field(min_length=3, max_length=500)


class KillSwitchView(StrictRequest):
    switch_id: str
    target_type: str
    target_code: str
    is_active: bool
    reason: str | None
    version: int


class KillSwitchList(StrictRequest):
    items: list[KillSwitchView]


class McpServerView(StrictRequest):
    server_code: str
    tools: list[str]
    timeout_seconds: float


class McpServerList(StrictRequest):
    items: list[McpServerView]


class AgentSkillBindingCreate(StrictRequest):
    skill_id: str = Field(min_length=1, max_length=40)
    skill_version_no: int = Field(ge=1)


class SkillToolBindingCreate(StrictRequest):
    tool_code: str = Field(min_length=3, max_length=128)
    tool_version_no: int = Field(ge=1)
    permission_effect: str = Field(pattern=r"^(allow|deny)$")
    confirmation_policy: str = Field(pattern=r"^(none|user_confirmation|required_approval)$")
    call_budget: int = Field(ge=0, le=100)
    timeout_ms: int = Field(ge=100, le=60000)


class VersionBindingView(StrictRequest):
    binding_id: int
    source_version_no: int
    target_code: str
    target_version_no: int
    effect: str
