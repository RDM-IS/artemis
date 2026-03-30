-- 005: Seed Gold-layer entities — known facts, not OSINT.

-- ── Person: Brian Pivar ───────────────────────────────────────
INSERT INTO acos.entities (
    entity_type, name, layer, domain, confidence,
    osint_source, content, tags, crm_contact_id
) VALUES (
    'Person',
    'Brian Pivar',
    'gold',
    'rdmis',
    1.0,
    NULL,
    'VP Enterprise Data & AI at TTI (Techtronic Industries). Primary Gate 2 contact for Lucint pilot. $125K pilot, 90-day delivery window. Meeting with Pivar = Gate 2 unlocked.',
    ARRAY['tier-1', 'gate-2', 'active-deal', 'tti', 'decision-maker'],
    (SELECT id FROM public.contacts WHERE name ILIKE '%Pivar%' OR email ILIKE '%pivar%' LIMIT 1)
)
ON CONFLICT DO NOTHING;

-- ── Organization: TTI ─────────────────────────────────────────
INSERT INTO acos.entities (
    entity_type, name, layer, domain, confidence,
    osint_source, content, tags
) VALUES (
    'Organization',
    'TTI (Techtronic Industries)',
    'gold',
    'rdmis',
    1.0,
    NULL,
    'Parent of Milwaukee Tool, Ryobi, AEG, Hoover. Primary Lucint pilot target. Industrial conglomerate. Gate 2 active.',
    ARRAY['active-deal', 'gate-2', 'anchor-client', 'industrial-conglomerate']
)
ON CONFLICT DO NOTHING;

-- ── Project: Lucint Pilot TTI ─────────────────────────────────
INSERT INTO acos.entities (
    entity_type, name, layer, domain, confidence,
    osint_source, content, tags
) VALUES (
    'Project',
    'Lucint Pilot TTI',
    'gold',
    'lucint',
    1.0,
    NULL,
    '$125K pilot. $62.5K on agreement, $62.5K on delivery. $2M savings guarantee. 90-day delivery window. Gate 1 complete. Gate 2 = secure meeting with Pivar.',
    ARRAY['gate-2', 'active', 'anchor', 'revenue-generating']
)
ON CONFLICT DO NOTHING;

-- ── Person: Bradley Spaits ────────────────────────────────────
INSERT INTO acos.entities (
    entity_type, name, layer, domain, confidence,
    osint_source, content, tags
) VALUES (
    'Person',
    'Bradley Spaits',
    'gold',
    'rdmis',
    1.0,
    NULL,
    'SCORE Milwaukee mentor. 35 years ERP experience. IBM, Infor regional VP. Weekly Thursday meetings 3pm CT. Validated Lucint thesis. Advisory board candidate.',
    ARRAY['mentor', 'score', 'advisory-board-candidate', 'weekly-meeting']
)
ON CONFLICT DO NOTHING;

-- ── Relationships ─────────────────────────────────────────────

-- Brian Pivar → TTI (Reports-to)
INSERT INTO acos.relationships (
    source_entity_id, target_entity_id,
    relationship_type, relationship_context,
    confidence, layer
)
SELECT
    (SELECT id FROM acos.entities WHERE name = 'Brian Pivar' AND entity_type = 'Person' LIMIT 1),
    (SELECT id FROM acos.entities WHERE name = 'TTI (Techtronic Industries)' AND entity_type = 'Organization' LIMIT 1),
    'Reports-to',
    'VP Enterprise Data & AI. Primary decision-maker for enterprise data platform procurement at TTI.',
    1.0,
    'gold'
WHERE EXISTS (SELECT 1 FROM acos.entities WHERE name = 'Brian Pivar')
  AND EXISTS (SELECT 1 FROM acos.entities WHERE name = 'TTI (Techtronic Industries)');

-- Brian Pivar → Lucint Pilot TTI (Blocks)
INSERT INTO acos.relationships (
    source_entity_id, target_entity_id,
    relationship_type, relationship_context,
    confidence, layer
)
SELECT
    (SELECT id FROM acos.entities WHERE name = 'Brian Pivar' AND entity_type = 'Person' LIMIT 1),
    (SELECT id FROM acos.entities WHERE name = 'Lucint Pilot TTI' AND entity_type = 'Project' LIMIT 1),
    'Blocks',
    'Gate 2 target. Securing a meeting with Pivar directly unlocks the pilot. No meeting = no pilot.',
    1.0,
    'gold'
WHERE EXISTS (SELECT 1 FROM acos.entities WHERE name = 'Brian Pivar')
  AND EXISTS (SELECT 1 FROM acos.entities WHERE name = 'Lucint Pilot TTI');

-- TTI → Lucint Pilot TTI (Belongs-to)
INSERT INTO acos.relationships (
    source_entity_id, target_entity_id,
    relationship_type, relationship_context,
    confidence, layer
)
SELECT
    (SELECT id FROM acos.entities WHERE name = 'TTI (Techtronic Industries)' AND entity_type = 'Organization' LIMIT 1),
    (SELECT id FROM acos.entities WHERE name = 'Lucint Pilot TTI' AND entity_type = 'Project' LIMIT 1),
    'Belongs-to',
    'Anchor client. Pilot defines RDMIS go-to-market and federal employment exit trigger.',
    1.0,
    'gold'
WHERE EXISTS (SELECT 1 FROM acos.entities WHERE name = 'TTI (Techtronic Industries)')
  AND EXISTS (SELECT 1 FROM acos.entities WHERE name = 'Lucint Pilot TTI');
