CREATE TABLE agent_adapters (
	agent_tool VARCHAR(32) NOT NULL, 
	version VARCHAR(32) NOT NULL, 
	runtime_spec JSONB NOT NULL, 
	health_endpoint VARCHAR(256), 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	CONSTRAINT pk_agent_adapters PRIMARY KEY (id), 
	CONSTRAINT tool_version UNIQUE (agent_tool, version)
)

CREATE TABLE audit_logs (
	actor_type VARCHAR(16) NOT NULL, 
	actor_id UUID, 
	action VARCHAR(64) NOT NULL, 
	resource_type VARCHAR(32), 
	resource_id VARCHAR(64), 
	params_digest JSONB, 
	result VARCHAR(16) NOT NULL, 
	ip INET, 
	latency_ms INTEGER, 
	trace_id VARCHAR(64), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	id UUID NOT NULL, 
	tenant_id UUID NOT NULL, 
	CONSTRAINT pk_audit_logs PRIMARY KEY (id), 
	CONSTRAINT ck_audit_logs_actor_type CHECK (actor_type IN ('user','api_key','agent','system'))
)

CREATE INDEX ix_audit_logs_trace_id ON audit_logs (trace_id)

CREATE INDEX ix_audit_tenant_time ON audit_logs (tenant_id, created_at)

CREATE INDEX ix_audit_logs_tenant_id ON audit_logs (tenant_id)

CREATE TABLE roles (
	code VARCHAR(32) NOT NULL, 
	name VARCHAR(64) NOT NULL, 
	scopes TEXT[] NOT NULL, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	CONSTRAINT pk_roles PRIMARY KEY (id), 
	CONSTRAINT uk_roles_code UNIQUE (code)
)

CREATE TABLE tenants (
	name VARCHAR(128) NOT NULL, 
	slug VARCHAR(64) NOT NULL, 
	plan VARCHAR(32) NOT NULL, 
	settings JSONB NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	CONSTRAINT pk_tenants PRIMARY KEY (id), 
	CONSTRAINT ck_tenants_plan CHECK (plan IN ('free','pro','ent')), 
	CONSTRAINT ck_tenants_status CHECK (status IN ('active','suspended')), 
	CONSTRAINT uk_tenants_slug UNIQUE (slug)
)

CREATE TABLE users (
	email VARCHAR(256) NOT NULL, 
	username VARCHAR(64), 
	password_hash VARCHAR(256) NOT NULL, 
	display_name VARCHAR(128), 
	last_login_at TIMESTAMP WITH TIME ZONE, 
	status VARCHAR(16) NOT NULL, 
	id UUID NOT NULL, 
	tenant_id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	CONSTRAINT pk_users PRIMARY KEY (id), 
	CONSTRAINT tenant_email UNIQUE (tenant_id, email)
)

CREATE INDEX ix_users_tenant_id ON users (tenant_id)

CREATE TABLE agents (
	name VARCHAR(128) NOT NULL, 
	agent_tool VARCHAR(32) NOT NULL, 
	system_prompt TEXT, 
	adapter_id UUID NOT NULL, 
	config JSONB NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	id UUID NOT NULL, 
	tenant_id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	CONSTRAINT pk_agents PRIMARY KEY (id), 
	CONSTRAINT tenant_name UNIQUE (tenant_id, name), 
	CONSTRAINT ck_agents_status CHECK (status IN ('enabled','disabled')), 
	CONSTRAINT fk_agents_adapter_id_agent_adapters FOREIGN KEY(adapter_id) REFERENCES agent_adapters (id)
)

CREATE INDEX ix_agents_agent_tool ON agents (agent_tool)

CREATE INDEX ix_agents_tenant_id ON agents (tenant_id)

CREATE TABLE api_keys (
	name VARCHAR(128) NOT NULL, 
	key_hash VARCHAR(128) NOT NULL, 
	key_prefix VARCHAR(16) NOT NULL, 
	owner_user_id UUID NOT NULL, 
	scopes TEXT[] NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE, 
	revoked_at TIMESTAMP WITH TIME ZONE, 
	last_used_at TIMESTAMP WITH TIME ZONE, 
	id UUID NOT NULL, 
	tenant_id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	CONSTRAINT pk_api_keys PRIMARY KEY (id), 
	CONSTRAINT uk_api_keys_key_prefix UNIQUE (key_prefix), 
	CONSTRAINT fk_api_keys_owner_user_id_users FOREIGN KEY(owner_user_id) REFERENCES users (id)
)

CREATE INDEX ix_api_keys_tenant_id ON api_keys (tenant_id)

CREATE INDEX ix_api_keys_owner_user_id ON api_keys (owner_user_id)

CREATE TABLE ontologies (
	iri_base VARCHAR(256) NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	description TEXT, 
	scheme_tier VARCHAR(16) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	format VARCHAR(16) NOT NULL, 
	owner_business UUID, 
	owner_engineer UUID, 
	current_version_id UUID, 
	id UUID NOT NULL, 
	tenant_id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	CONSTRAINT pk_ontologies PRIMARY KEY (id), 
	CONSTRAINT tenant_iri UNIQUE (tenant_id, iri_base), 
	CONSTRAINT ck_ontologies_scheme_tier CHECK (scheme_tier IN ('glossary','light_graph','heavy')), 
	CONSTRAINT ck_ontologies_status CHECK (status IN ('draft','published','deprecated')), 
	CONSTRAINT fk_ontologies_owner_business_users FOREIGN KEY(owner_business) REFERENCES users (id), 
	CONSTRAINT fk_ontologies_owner_engineer_users FOREIGN KEY(owner_engineer) REFERENCES users (id)
)

CREATE INDEX ix_ontologies_tenant_id ON ontologies (tenant_id)

CREATE TABLE user_roles (
	user_id UUID NOT NULL, 
	role_id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	id UUID NOT NULL, 
	tenant_id UUID NOT NULL, 
	CONSTRAINT pk_user_roles PRIMARY KEY (id), 
	CONSTRAINT user_role UNIQUE (user_id, role_id), 
	CONSTRAINT fk_user_roles_user_id_users FOREIGN KEY(user_id) REFERENCES users (id), 
	CONSTRAINT fk_user_roles_role_id_roles FOREIGN KEY(role_id) REFERENCES roles (id)
)

CREATE INDEX ix_user_roles_tenant_id ON user_roles (tenant_id)

CREATE TABLE kb_collections (
	name VARCHAR(128) NOT NULL, 
	description TEXT, 
	ontology_id UUID, 
	embedding_model VARCHAR(64) NOT NULL, 
	chunk_defaults JSONB NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	id UUID NOT NULL, 
	tenant_id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	CONSTRAINT pk_kb_collections PRIMARY KEY (id), 
	CONSTRAINT tenant_name UNIQUE (tenant_id, name), 
	CONSTRAINT ck_kb_collections_status CHECK (status IN ('active','archived')), 
	CONSTRAINT fk_kb_collections_ontology_id_ontologies FOREIGN KEY(ontology_id) REFERENCES ontologies (id)
)

CREATE INDEX ix_kb_collections_tenant_id ON kb_collections (tenant_id)

CREATE TABLE ontology_versions (
	tenant_id UUID NOT NULL, 
	ontology_id UUID NOT NULL, 
	version VARCHAR(32) NOT NULL, 
	version_no INTEGER NOT NULL, 
	artifact_key VARCHAR(512) NOT NULL, 
	checksum CHAR(64) NOT NULL, 
	triple_count INTEGER, 
	changelog TEXT, 
	parent_version_id UUID, 
	published_by UUID, 
	published_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	id UUID NOT NULL, 
	CONSTRAINT pk_ontology_versions PRIMARY KEY (id), 
	CONSTRAINT ontology_version UNIQUE (ontology_id, version), 
	CONSTRAINT ontology_version_no UNIQUE (ontology_id, version_no), 
	CONSTRAINT fk_ontology_versions_ontology_id_ontologies FOREIGN KEY(ontology_id) REFERENCES ontologies (id), 
	CONSTRAINT fk_ontology_versions_parent_version_id_ontology_versions FOREIGN KEY(parent_version_id) REFERENCES ontology_versions (id), 
	CONSTRAINT fk_ontology_versions_published_by_users FOREIGN KEY(published_by) REFERENCES users (id)
)

CREATE INDEX ix_ontology_versions_tenant_id ON ontology_versions (tenant_id)

CREATE INDEX ix_ontology_versions_ontology_id ON ontology_versions (ontology_id)

CREATE TABLE sessions (
	agent_id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	title VARCHAR(256), 
	channel VARCHAR(32) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	last_message_at TIMESTAMP WITH TIME ZONE, 
	token_usage JSONB NOT NULL, 
	id UUID NOT NULL, 
	tenant_id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	CONSTRAINT pk_sessions PRIMARY KEY (id), 
	CONSTRAINT ck_sessions_channel CHECK (channel IN ('web','api','cli')), 
	CONSTRAINT ck_sessions_status CHECK (status IN ('created','active','idle','closed','archived')), 
	CONSTRAINT fk_sessions_agent_id_agents FOREIGN KEY(agent_id) REFERENCES agents (id), 
	CONSTRAINT fk_sessions_user_id_users FOREIGN KEY(user_id) REFERENCES users (id)
)

CREATE INDEX ix_sessions_tenant_user_recent ON sessions (tenant_id, user_id, last_message_at)

CREATE INDEX ix_sessions_tenant_id ON sessions (tenant_id)

CREATE TABLE documents (
	kb_collection_id UUID NOT NULL, 
	title VARCHAR(512) NOT NULL, 
	source_type VARCHAR(16) NOT NULL, 
	mime_type VARCHAR(128), 
	size_bytes INTEGER, 
	minio_key VARCHAR(512) NOT NULL, 
	checksum_sha256 CHAR(64) NOT NULL, 
	meta JSONB NOT NULL, 
	valid_from TIMESTAMP WITH TIME ZONE, 
	valid_to TIMESTAMP WITH TIME ZONE, 
	status VARCHAR(16) NOT NULL, 
	id UUID NOT NULL, 
	tenant_id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	CONSTRAINT pk_documents PRIMARY KEY (id), 
	CONSTRAINT ck_documents_source_type CHECK (source_type IN ('upload','api')), 
	CONSTRAINT ck_documents_status CHECK (status IN ('uploaded','preprocessed','extracting','aligning','validating','pending_review','indexed','failed')), 
	CONSTRAINT tenant_kb_checksum UNIQUE (tenant_id, kb_collection_id, checksum_sha256), 
	CONSTRAINT fk_documents_kb_collection_id_kb_collections FOREIGN KEY(kb_collection_id) REFERENCES kb_collections (id)
)

CREATE INDEX ix_documents_current ON documents (tenant_id, kb_collection_id) WHERE valid_to IS NULL

CREATE INDEX ix_documents_tenant_id ON documents (tenant_id)

CREATE INDEX ix_documents_status ON documents (status)

CREATE TABLE messages (
	session_id UUID NOT NULL, 
	seq INTEGER NOT NULL, 
	role VARCHAR(16) NOT NULL, 
	content TEXT NOT NULL, 
	content_type VARCHAR(32) NOT NULL, 
	ag_ui_events JSONB, 
	model VARCHAR(64), 
	token_in INTEGER, 
	token_out INTEGER, 
	latency_ms INTEGER, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	id UUID NOT NULL, 
	tenant_id UUID NOT NULL, 
	CONSTRAINT pk_messages PRIMARY KEY (id), 
	CONSTRAINT session_seq UNIQUE (session_id, seq), 
	CONSTRAINT ck_messages_role CHECK (role IN ('user','assistant','tool','system')), 
	CONSTRAINT fk_messages_session_id_sessions FOREIGN KEY(session_id) REFERENCES sessions (id)
)

CREATE INDEX ix_messages_tenant_id ON messages (tenant_id)

CREATE TABLE tasks (
	type VARCHAR(32) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	agent_id UUID, 
	session_id UUID, 
	attempt_count INTEGER NOT NULL, 
	active_run_id UUID, 
	payload JSONB, 
	result JSONB, 
	error TEXT, 
	priority SMALLINT NOT NULL, 
	idempotency_key VARCHAR(128), 
	started_at TIMESTAMP WITH TIME ZONE, 
	finished_at TIMESTAMP WITH TIME ZONE, 
	id UUID NOT NULL, 
	tenant_id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	CONSTRAINT pk_tasks PRIMARY KEY (id), 
	CONSTRAINT ck_tasks_status CHECK (status IN ('pending','running','succeeded','failed','cancelled')), 
	CONSTRAINT tenant_idem UNIQUE (tenant_id, idempotency_key), 
	CONSTRAINT fk_tasks_agent_id_agents FOREIGN KEY(agent_id) REFERENCES agents (id), 
	CONSTRAINT fk_tasks_session_id_sessions FOREIGN KEY(session_id) REFERENCES sessions (id)
)

CREATE INDEX ix_tasks_tenant_id ON tasks (tenant_id)

CREATE UNIQUE INDEX uk_tasks_one_active_run ON tasks (tenant_id, session_id) WHERE status = 'running'

CREATE INDEX ix_tasks_queue ON tasks (status, priority, created_at)

CREATE TABLE runs (
	task_id UUID NOT NULL, 
	seq_start INTEGER NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	started_at TIMESTAMP WITH TIME ZONE, 
	ended_at TIMESTAMP WITH TIME ZONE, 
	usage JSONB NOT NULL, 
	error JSONB, 
	id UUID NOT NULL, 
	tenant_id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	CONSTRAINT pk_runs PRIMARY KEY (id), 
	CONSTRAINT ck_runs_status CHECK (status IN ('queued','running','waiting_tool','completed','failed','timeout','cancelled')), 
	CONSTRAINT fk_runs_task_id_tasks FOREIGN KEY(task_id) REFERENCES tasks (id)
)

CREATE INDEX ix_runs_task_id ON runs (task_id)

CREATE UNIQUE INDEX uk_runs_one_active ON runs (tenant_id, task_id) WHERE status IN ('queued','running','waiting_tool')

CREATE INDEX ix_runs_tenant_id ON runs (tenant_id)

CREATE TABLE task_events (
	task_id UUID NOT NULL, 
	seq INTEGER NOT NULL, 
	event_type VARCHAR(32) NOT NULL, 
	data JSONB NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	id UUID NOT NULL, 
	tenant_id UUID NOT NULL, 
	CONSTRAINT pk_task_events PRIMARY KEY (id), 
	CONSTRAINT task_seq UNIQUE (task_id, seq), 
	CONSTRAINT fk_task_events_task_id_tasks FOREIGN KEY(task_id) REFERENCES tasks (id)
)

CREATE INDEX ix_task_events_tenant_id ON task_events (tenant_id)