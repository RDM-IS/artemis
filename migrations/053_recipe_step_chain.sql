-- 053_recipe_step_chain.sql
-- PREP-2 fix: steps within a recipe are not always one queue.
--
-- FOUND BY RUNNING IT, NOT BY A TEST. The seeded Lentil tofu bowl has eleven
-- steps, and migration 052's model made step N depend on step N-1 throughout — so
-- the lentils, the vegetables and the tofu were cooked strictly one after another.
-- The board said **125 minutes with a 25-minute idle gap**; the same work in
-- parallel is about an hour. The synthetic test batch never showed it, because it
-- modelled those three as separate recipes, which is exactly how the source spec
-- lists them.
--
-- `chain_key` names an independent line of work inside one recipe. Steps sharing a
-- key are sequential; different keys run in parallel.
--
-- A step with chain_key IS NULL is a BARRIER: it waits for every chain, and every
-- chain then continues after it. That is what "Portion" is — you cannot portion
-- until the lentils, the veg and the tofu are all done — and it means the common
-- case (a recipe that really is one queue) needs no keys at all and behaves
-- exactly as before.
--
-- COLUMN-GREP: adds one nullable column, drops and renames nothing. Existing rows
-- get NULL, which is the old behaviour — every step a barrier, therefore strictly
-- sequential.

ALTER TABLE nutrition.recipe_step ADD COLUMN IF NOT EXISTS chain_key TEXT;

COMMENT ON COLUMN nutrition.recipe_step.chain_key IS
  'Independent line of work within the recipe. Same key = sequential; different '
  'keys run in parallel; NULL = a barrier that waits for every chain.';
