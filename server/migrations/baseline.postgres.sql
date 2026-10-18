CREATE TABLE admin_sessions (
	token_hash VARCHAR(64) NOT NULL, 
	auth_method VARCHAR(16) NOT NULL, 
	actor_discord_user_id VARCHAR(20), 
	credential_binding VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (token_hash)
);

CREATE TABLE discord_viewer_grant_events (
	id SERIAL NOT NULL, 
	discord_user_id VARCHAR(20) NOT NULL, 
	actor_method VARCHAR(16) NOT NULL, 
	actor_discord_user_id VARCHAR(20), 
	actor_session_hash VARCHAR(64) NOT NULL, 
	action VARCHAR(16) DEFAULT 'grant' NOT NULL, 
	replacement_user_id VARCHAR(20), 
	previous_role VARCHAR(16), 
	new_role VARCHAR(16), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_discord_viewer_grant_events_discord_user_id ON discord_viewer_grant_events (discord_user_id);

CREATE TABLE discord_viewers (
	discord_user_id VARCHAR(20) NOT NULL, 
	is_admin BOOLEAN DEFAULT 'false' NOT NULL, 
	generation VARCHAR(32) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (discord_user_id)
);

CREATE TABLE import_batches (
	id VARCHAR(32) NOT NULL, 
	filename VARCHAR(255) NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	row_count INTEGER NOT NULL, 
	message_count INTEGER NOT NULL, 
	review_count INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	applied_at TIMESTAMP WITH TIME ZONE, 
	error TEXT, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_import_batches_status CHECK (status IN ('validated','applied','failed')), 
	UNIQUE (sha256)
);

CREATE TABLE login_attempts (
	key VARCHAR(64) NOT NULL, 
	failures INTEGER NOT NULL, 
	window_started BIGINT NOT NULL, 
	blocked_until BIGINT NOT NULL, 
	PRIMARY KEY (key)
);

CREATE INDEX ix_login_attempts_window_started ON login_attempts (window_started);

CREATE TABLE lookup_cache (
	cache_key VARCHAR(64) NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	payload TEXT NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (cache_key)
);

CREATE INDEX ix_lookup_cache_expires_at ON lookup_cache (expires_at);

CREATE INDEX ix_lookup_cache_kind ON lookup_cache (kind);

CREATE TABLE messages (
	message_id VARCHAR(20) NOT NULL, 
	channel_id VARCHAR(20), 
	content TEXT, 
	source_created_at TIMESTAMP WITH TIME ZONE, 
	is_target BOOLEAN NOT NULL, 
	processing_status VARCHAR(16) NOT NULL, 
	fetch_error TEXT, 
	processed_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (message_id), 
	CONSTRAINT ck_messages_id_length CHECK (length(message_id) BETWEEN 17 AND 20), 
	CONSTRAINT ck_messages_processing_status CHECK (processing_status IN ('pending','processing','succeeded','failed','ignored'))
);

CREATE INDEX ix_messages_channel_id ON messages (channel_id);

CREATE INDEX ix_messages_processing_status ON messages (processing_status);

CREATE TABLE shops (
	id SERIAL NOT NULL, 
	shop_name VARCHAR NOT NULL, 
	branch_name VARCHAR(255), 
	area VARCHAR, 
	category VARCHAR, 
	address TEXT, 
	phone VARCHAR(32), 
	canonical_url TEXT, 
	image_key VARCHAR(64), 
	external_source VARCHAR(64), 
	external_id VARCHAR(255), 
	is_visited BOOLEAN NOT NULL, 
	visited_at TIMESTAMP WITH TIME ZONE, 
	rating INTEGER, 
	memo TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	version INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_shops_rating CHECK (rating IS NULL OR rating BETWEEN 1 AND 5), 
	CONSTRAINT ck_shops_external_identity_pair CHECK ((external_source IS NULL AND external_id IS NULL) OR (external_source IS NOT NULL AND external_id IS NOT NULL)), 
	CONSTRAINT uq_shops_external_identity UNIQUE (external_source, external_id)
);

CREATE INDEX ix_shops_image_key ON shops (image_key);

CREATE INDEX ix_shops_phone ON shops (phone);

CREATE TABLE sync_states (
	channel_id VARCHAR(20) NOT NULL, 
	last_contiguous_message_id VARCHAR(20), 
	last_weekly_sync_started_at TIMESTAMP WITH TIME ZONE, 
	last_weekly_sync_completed_at TIMESTAMP WITH TIME ZONE, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (channel_id)
);

CREATE TABLE import_rows (
	id SERIAL NOT NULL, 
	batch_id VARCHAR(32) NOT NULL, 
	row_number INTEGER NOT NULL, 
	shop_id INTEGER NOT NULL, 
	message_id VARCHAR(20) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	shop_name VARCHAR NOT NULL, 
	branch_name VARCHAR(255), 
	area VARCHAR, 
	category VARCHAR, 
	address TEXT, 
	phone VARCHAR(32), 
	external_source VARCHAR(64), 
	external_id VARCHAR(255), 
	is_visited BOOLEAN NOT NULL, 
	visited_at TIMESTAMP WITH TIME ZONE, 
	rating INTEGER, 
	memo TEXT, 
	source_url TEXT, 
	canonical_url TEXT, 
	image_key VARCHAR(64), 
	needs_review BOOLEAN NOT NULL, 
	resolution_status VARCHAR(16) NOT NULL, 
	review_status VARCHAR(16) NOT NULL, 
	metadata_review_status VARCHAR(16) NOT NULL, 
	resolution_method VARCHAR(16), 
	difference_type VARCHAR(64), 
	metadata_difference_type VARCHAR(64), 
	reviewed_at TIMESTAMP WITH TIME ZONE, 
	metadata_reviewed_at TIMESTAMP WITH TIME ZONE, 
	extraction_source VARCHAR(64) NOT NULL, 
	extraction_error TEXT, 
	confidence_reason TEXT, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_import_rows_batch_row UNIQUE (batch_id, row_number), 
	CONSTRAINT uq_import_rows_batch_shop UNIQUE (batch_id, shop_id), 
	CONSTRAINT ck_import_rows_rating CHECK (rating IS NULL OR rating BETWEEN 1 AND 5), 
	CONSTRAINT ck_import_rows_resolution_status CHECK (resolution_status IN ('resolved','ambiguous','not_found','invalid')), 
	CONSTRAINT ck_import_rows_review_status CHECK (review_status IN ('pending','approved','rejected','deferred')), 
	CONSTRAINT ck_import_rows_metadata_review_status CHECK (metadata_review_status IN ('pending','approved','deferred')), 
	CONSTRAINT ck_import_rows_resolution_method CHECK (resolution_method IS NULL OR resolution_method IN ('automatic','manual')), 
	FOREIGN KEY(batch_id) REFERENCES import_batches (id) ON DELETE CASCADE
);

CREATE TABLE processing_runs (
	id SERIAL NOT NULL, 
	message_id VARCHAR(20), 
	stage VARCHAR(64) NOT NULL, 
	model VARCHAR(128), 
	prompt_version VARCHAR(64), 
	status VARCHAR(16) NOT NULL, 
	input_tokens INTEGER NOT NULL, 
	output_tokens INTEGER NOT NULL, 
	web_search_calls INTEGER NOT NULL, 
	image_count INTEGER NOT NULL, 
	latency_ms INTEGER NOT NULL, 
	estimated_cost_microusd INTEGER NOT NULL, 
	error TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_processing_runs_status CHECK (status IN ('pending','processing','succeeded','failed','ignored')), 
	FOREIGN KEY(message_id) REFERENCES messages (message_id) ON DELETE SET NULL
);

CREATE TABLE shop_mentions (
	id SERIAL NOT NULL, 
	message_id VARCHAR(20) NOT NULL, 
	shop_id INTEGER, 
	occurrence_index INTEGER NOT NULL, 
	extracted_name VARCHAR NOT NULL, 
	extracted_branch_name VARCHAR(255), 
	extracted_area VARCHAR, 
	extracted_category VARCHAR, 
	source_url TEXT, 
	resolution_status VARCHAR(16) NOT NULL, 
	review_status VARCHAR(16) NOT NULL, 
	metadata_review_status VARCHAR(16) NOT NULL, 
	metadata_difference_type VARCHAR(64), 
	resolution_method VARCHAR(16), 
	difference_type VARCHAR(64), 
	extraction_source VARCHAR(64) NOT NULL, 
	extraction_error TEXT, 
	confidence_reason TEXT, 
	reviewed_at TIMESTAMP WITH TIME ZONE, 
	metadata_reviewed_at TIMESTAMP WITH TIME ZONE, 
	reused_from_mention_id INTEGER, 
	resolution_basis VARCHAR(32), 
	version INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_mentions_message_occurrence UNIQUE (message_id, occurrence_index), 
	CONSTRAINT ck_mentions_resolution_status CHECK (resolution_status IN ('resolved','ambiguous','not_found','invalid')), 
	CONSTRAINT ck_mentions_review_status CHECK (review_status IN ('pending','approved','rejected','deferred')), 
	CONSTRAINT ck_mentions_metadata_review_status CHECK (metadata_review_status IN ('pending','approved','deferred')), 
	CONSTRAINT ck_mentions_resolution_method CHECK (resolution_method IS NULL OR resolution_method IN ('automatic','manual')), 
	CONSTRAINT ck_mentions_resolution_basis CHECK (resolution_basis IS NULL OR resolution_basis IN ('source_reuse','existing_shop','verified_candidate','verified_collision','image_evidence')), 
	FOREIGN KEY(message_id) REFERENCES messages (message_id) ON DELETE CASCADE, 
	FOREIGN KEY(shop_id) REFERENCES shops (id) ON DELETE SET NULL, 
	CONSTRAINT fk_shop_mentions_reused_from FOREIGN KEY(reused_from_mention_id) REFERENCES shop_mentions (id) ON DELETE SET NULL
);

CREATE INDEX ix_shop_mentions_difference_type ON shop_mentions (difference_type);

CREATE INDEX ix_shop_mentions_metadata_difference_type ON shop_mentions (metadata_difference_type);

CREATE INDEX ix_shop_mentions_metadata_review_status ON shop_mentions (metadata_review_status);

CREATE INDEX ix_shop_mentions_resolution_basis ON shop_mentions (resolution_basis);

CREATE INDEX ix_shop_mentions_resolution_status ON shop_mentions (resolution_status);

CREATE INDEX ix_shop_mentions_reused_from_mention_id ON shop_mentions (reused_from_mention_id);

CREATE INDEX ix_shop_mentions_review_status ON shop_mentions (review_status);

CREATE INDEX ix_shop_mentions_shop_id ON shop_mentions (shop_id);

CREATE TABLE shop_redirects (
	source_shop_id SERIAL NOT NULL, 
	target_shop_id INTEGER NOT NULL, 
	reason VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (source_shop_id), 
	CONSTRAINT ck_shop_redirects_distinct CHECK (source_shop_id <> target_shop_id), 
	FOREIGN KEY(target_shop_id) REFERENCES shops (id) ON DELETE RESTRICT
);

CREATE INDEX ix_shop_redirects_target_shop_id ON shop_redirects (target_shop_id);

CREATE TABLE source_assets (
	id SERIAL NOT NULL, 
	message_id VARCHAR(20) NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	url TEXT NOT NULL, 
	title TEXT, 
	description TEXT, 
	mime_type VARCHAR(255), 
	extracted_text TEXT, 
	source_service VARCHAR(32), 
	source_item_id VARCHAR(255), 
	normalized_url TEXT, 
	content_fingerprint VARCHAR(64), 
	fetch_status VARCHAR(16) NOT NULL, 
	fetch_error TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_source_assets_kind CHECK (kind IN ('link','embed','image','attachment')), 
	CONSTRAINT ck_source_assets_fetch_status CHECK (fetch_status IN ('pending','available','unavailable')), 
	CONSTRAINT ck_source_assets_identity_pair CHECK ((source_service IS NULL AND source_item_id IS NULL) OR (source_service IS NOT NULL AND source_item_id IS NOT NULL)), 
	CONSTRAINT uq_source_assets_message_kind_url UNIQUE (message_id, kind, url), 
	FOREIGN KEY(message_id) REFERENCES messages (message_id) ON DELETE CASCADE
);

CREATE INDEX ix_source_assets_content_fingerprint ON source_assets (content_fingerprint);

CREATE INDEX ix_source_assets_normalized_url ON source_assets (normalized_url);

CREATE INDEX ix_source_assets_source_identity ON source_assets (source_service, source_item_id, normalized_url);

CREATE TABLE resolution_candidates (
	id SERIAL NOT NULL, 
	mention_id INTEGER NOT NULL, 
	rank INTEGER NOT NULL, 
	name VARCHAR NOT NULL, 
	area VARCHAR, 
	category VARCHAR, 
	address TEXT, 
	phone VARCHAR(32), 
	canonical_url TEXT, 
	external_source VARCHAR(64), 
	external_id VARCHAR(255), 
	evidence_url TEXT, 
	provenance VARCHAR(32) NOT NULL, 
	is_verified BOOLEAN NOT NULL, 
	verification_reason TEXT, 
	matched_fields TEXT, 
	conflicting_fields TEXT, 
	name_similarity_milli INTEGER NOT NULL, 
	is_strong_match BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_candidates_similarity CHECK (name_similarity_milli BETWEEN 0 AND 1000), 
	CONSTRAINT ck_candidates_provenance CHECK (provenance IN ('posted_url','structured_data','web_search','image')), 
	CONSTRAINT uq_candidates_mention_rank UNIQUE (mention_id, rank), 
	FOREIGN KEY(mention_id) REFERENCES shop_mentions (id) ON DELETE CASCADE
);

CREATE TABLE review_events (
	id SERIAL NOT NULL, 
	mention_id INTEGER NOT NULL, 
	scope VARCHAR(16) NOT NULL, 
	action VARCHAR(32) NOT NULL, 
	previous_shop_id INTEGER, 
	selected_shop_id INTEGER, 
	note TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_review_events_scope CHECK (scope IN ('identity','metadata')), 
	FOREIGN KEY(mention_id) REFERENCES shop_mentions (id) ON DELETE CASCADE
);
