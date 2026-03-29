-- 003: Create indexes on acos tables

-- entities
CREATE INDEX IF NOT EXISTS idx_entities_type_layer_domain
    ON acos.entities (entity_type, layer, domain);
CREATE INDEX IF NOT EXISTS idx_entities_tags
    ON acos.entities USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_entities_crm_contact_id
    ON acos.entities (crm_contact_id);
CREATE INDEX IF NOT EXISTS idx_entities_novelty_score
    ON acos.entities (novelty_score);
CREATE INDEX IF NOT EXISTS idx_entities_osint_source
    ON acos.entities (osint_source);

-- relationships
CREATE INDEX IF NOT EXISTS idx_relationships_source_target
    ON acos.relationships (source_entity_id, target_entity_id);
CREATE INDEX IF NOT EXISTS idx_relationships_type_layer
    ON acos.relationships (relationship_type, layer);

-- osint_signals
CREATE INDEX IF NOT EXISTS idx_osint_entity_source_processed
    ON acos.osint_signals (entity_id, source, processed, layer);
CREATE INDEX IF NOT EXISTS idx_osint_corroboration
    ON acos.osint_signals (corroboration_count);

-- data_vault_satellites
CREATE INDEX IF NOT EXISTS idx_satellites_entity_type_sync
    ON acos.data_vault_satellites (entity_id, satellite_type, crm_syncable);

-- audit_log
CREATE INDEX IF NOT EXISTS idx_audit_agent_created_domain
    ON acos.audit_log (agent, created_at, domain);
CREATE INDEX IF NOT EXISTS idx_audit_outcome
    ON acos.audit_log (outcome);

-- velocity_ledger (EventBridge 1-hour rolling window queries)
CREATE INDEX IF NOT EXISTS idx_velocity_agent_action_created
    ON acos.velocity_ledger (agent, action_type, created_at);

-- circuit_breaker_status
CREATE INDEX IF NOT EXISTS idx_circuit_agent_suspended
    ON acos.circuit_breaker_status (agent, suspended_at);
