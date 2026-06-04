-- 009: Seed pipeline accounts, Greg Weddle contact, and acos entities
-- Idempotent: uses ON CONFLICT or WHERE NOT EXISTS checks
--
-- NOTE: public.organizations, public.contacts, public.deals are created
-- by SQLAlchemy (api/app/models.py) which uses Python-level defaults for
-- UUID id columns — NOT SQL-level DEFAULT gen_random_uuid(). Every raw
-- SQL INSERT must provide gen_random_uuid() explicitly for the id column.

-- ═══════════════════════════════════════════════════════════════
-- Organizations (must be inserted before deals and contacts)
-- ═══════════════════════════════════════════════════════════════

INSERT INTO public.organizations (id, name, type, industry, notes)
SELECT gen_random_uuid(), 'Dover Corporation', 'prospect', 'Industrial', 'Tier 1 target. Diversified industrial manufacturer.'
WHERE NOT EXISTS (SELECT 1 FROM public.organizations WHERE LOWER(name) = 'dover corporation');

INSERT INTO public.organizations (id, name, type, industry, notes)
SELECT gen_random_uuid(), 'TTI / Milwaukee Tool', 'prospect', 'Industrial', 'Tier 1 anchor. Power tools and outdoor products.'
WHERE NOT EXISTS (SELECT 1 FROM public.organizations WHERE LOWER(name) = 'tti / milwaukee tool');

INSERT INTO public.organizations (id, name, type, industry, notes)
SELECT gen_random_uuid(), 'Acme Corp', 'prospect', 'Industrial', 'Demo account. CPO Jane Smith.'
WHERE NOT EXISTS (SELECT 1 FROM public.organizations WHERE LOWER(name) = 'acme corp');

INSERT INTO public.organizations (id, name, type, industry, notes)
SELECT gen_random_uuid(), 'Stanley Black & Decker', 'prospect', 'Industrial', 'Tier 2. Use TTI outcomes as reference.'
WHERE NOT EXISTS (SELECT 1 FROM public.organizations WHERE LOWER(name) = 'stanley black & decker');

INSERT INTO public.organizations (id, name, type, industry, notes)
SELECT gen_random_uuid(), 'Fortive', 'prospect', 'Industrial', 'Tier 2. FBS culture, Forseti fit.'
WHERE NOT EXISTS (SELECT 1 FROM public.organizations WHERE LOWER(name) = 'fortive');

INSERT INTO public.organizations (id, name, type, industry, notes)
SELECT gen_random_uuid(), 'Illinois Tool Works', 'prospect', 'Industrial', 'Tier 3. Deprioritized — federated model.'
WHERE NOT EXISTS (SELECT 1 FROM public.organizations WHERE LOWER(name) = 'illinois tool works');

-- ═══════════════════════════════════════════════════════════════
-- Deals (linked to orgs above — orgs must exist first)
-- ═══════════════════════════════════════════════════════════════

INSERT INTO public.deals (id, org_id, name, gate, stage, value, notes)
SELECT gen_random_uuid(), o.id, 'Dover — Forseti Pilot', 0, 'Research',
       125000.00, 'Tier 1. Greg Weddle CSCO. Warm path via Brad Spaits.'
FROM public.organizations o WHERE LOWER(o.name) = 'dover corporation'
AND NOT EXISTS (
    SELECT 1 FROM public.deals d
    JOIN public.organizations o2 ON d.org_id = o2.id
    WHERE LOWER(o2.name) = 'dover corporation'
);

INSERT INTO public.deals (id, org_id, name, gate, stage, value, notes)
SELECT gen_random_uuid(), o.id, 'TTI — Forseti Pilot', 1, 'Outreach',
       125000.00, 'Tier 1 anchor. Brian Pivar DM sent March 26.'
FROM public.organizations o WHERE LOWER(o.name) = 'tti / milwaukee tool'
AND NOT EXISTS (
    SELECT 1 FROM public.deals d
    JOIN public.organizations o2 ON d.org_id = o2.id
    WHERE LOWER(o2.name) = 'tti / milwaukee tool'
);

INSERT INTO public.deals (id, org_id, name, gate, stage, value, notes)
SELECT gen_random_uuid(), o.id, 'Acme — Forseti Demo', 2, 'Meeting',
       80000.00, 'Demo April 7. CPO Jane Smith. 6-10 person.'
FROM public.organizations o WHERE LOWER(o.name) = 'acme corp'
AND NOT EXISTS (
    SELECT 1 FROM public.deals d
    JOIN public.organizations o2 ON d.org_id = o2.id
    WHERE LOWER(o2.name) = 'acme corp'
);

INSERT INTO public.deals (id, org_id, name, gate, stage, value, notes)
SELECT gen_random_uuid(), o.id, 'SBD — Forseti Pilot', 0, 'Research',
       125000.00, 'Tier 2. Use TTI outcomes as reference.'
FROM public.organizations o WHERE LOWER(o.name) = 'stanley black & decker'
AND NOT EXISTS (
    SELECT 1 FROM public.deals d
    JOIN public.organizations o2 ON d.org_id = o2.id
    WHERE LOWER(o2.name) = 'stanley black & decker'
);

INSERT INTO public.deals (id, org_id, name, gate, stage, value, notes)
SELECT gen_random_uuid(), o.id, 'Fortive — Forseti Pilot', 0, 'Research',
       125000.00, 'Tier 2. FBS culture, Forseti fit.'
FROM public.organizations o WHERE LOWER(o.name) = 'fortive'
AND NOT EXISTS (
    SELECT 1 FROM public.deals d
    JOIN public.organizations o2 ON d.org_id = o2.id
    WHERE LOWER(o2.name) = 'fortive'
);

INSERT INTO public.deals (id, org_id, name, gate, stage, value, notes)
SELECT gen_random_uuid(), o.id, 'ITW — Forseti Pilot', 0, 'Research',
       125000.00, 'Tier 3. Deprioritized — federated model.'
FROM public.organizations o WHERE LOWER(o.name) = 'illinois tool works'
AND NOT EXISTS (
    SELECT 1 FROM public.deals d
    JOIN public.organizations o2 ON d.org_id = o2.id
    WHERE LOWER(o2.name) = 'illinois tool works'
);

-- ═══════════════════════════════════════════════════════════════
-- Greg Weddle — contact + entity + relationship
-- Contact must be inserted before entity (entity refs crm_contact_id)
-- Entities must be inserted before relationship (FKs)
-- ═══════════════════════════════════════════════════════════════

INSERT INTO public.contacts (id, name, title, phone, notes, org_id)
SELECT
    gen_random_uuid(),
    'Greg Weddle',
    'VP Global Supply Chain / CSCO',
    NULL,
    '16 years Dover. Active CSCO since 2022. 3rd degree on LinkedIn. Warm path via Brad Spaits April 3.',
    o.id
FROM public.organizations o WHERE LOWER(o.name) = 'dover corporation'
AND NOT EXISTS (SELECT 1 FROM public.contacts WHERE LOWER(name) = 'greg weddle');

-- acos entity: Greg Weddle (Person)
-- acos.entities has DEFAULT gen_random_uuid() in DDL — no explicit id needed
INSERT INTO acos.entities (entity_type, name, domain, content, confidence, layer, tags, crm_contact_id)
SELECT
    'Person',
    'Greg Weddle',
    'rdmis',
    'VP Global Supply Chain / CSCO at Dover Corporation. 16 years at Dover. Active CSCO since 2022. Warm path via Brad Spaits.',
    0.8,
    'silver',
    ARRAY['imported', 'pipeline'],
    c.id
FROM public.contacts c WHERE LOWER(c.name) = 'greg weddle'
AND NOT EXISTS (SELECT 1 FROM acos.entities WHERE LOWER(name) = 'greg weddle' AND entity_type = 'Person');

-- acos entity: Dover Corporation (Organization)
INSERT INTO acos.entities (entity_type, name, domain, content, confidence, layer)
SELECT
    'Organization',
    'Dover Corporation',
    'rdmis',
    'Diversified industrial manufacturer. Tier 1 pipeline target for Forseti.',
    0.8,
    'silver'
WHERE NOT EXISTS (SELECT 1 FROM acos.entities WHERE LOWER(name) = 'dover corporation' AND entity_type = 'Organization');

-- acos relationship: Weddle Works-at Dover
-- Both entities must exist first (FK constraints on source/target)
INSERT INTO acos.relationships (source_entity_id, target_entity_id, relationship_type, relationship_context, confidence, layer)
SELECT
    p.id,
    o.id,
    'Works-at',
    'Greg Weddle is VP Global Supply Chain / CSCO at Dover Corporation',
    0.8,
    'silver'
FROM acos.entities p, acos.entities o
WHERE p.name = 'Greg Weddle' AND p.entity_type = 'Person'
  AND o.name = 'Dover Corporation' AND o.entity_type = 'Organization'
AND NOT EXISTS (
    SELECT 1 FROM acos.relationships r
    WHERE r.source_entity_id = p.id AND r.target_entity_id = o.id
      AND r.relationship_type = 'Works-at'
);
