-- 004: Cross-schema view joining acos entities with crm contacts
-- Reads from crm schema but does NOT modify it.

CREATE OR REPLACE VIEW acos.v_gold_contacts AS
SELECT
    e.id              AS entity_id,
    e.name            AS entity_name,
    e.confidence      AS entity_confidence,
    e.tags            AS entity_tags,
    e.domain          AS entity_domain,
    e.content         AS entity_content,
    e.novelty_score   AS entity_novelty_score,
    c.id              AS crm_id,
    c.email           AS crm_email,
    c.org_id          AS crm_organization,
    c.title           AS crm_tier,
    c.last_contacted  AS crm_last_interaction_at
FROM acos.entities e
LEFT JOIN public.contacts c ON e.crm_contact_id = c.id
WHERE e.layer = 'gold'
  AND e.entity_type = 'Person';
