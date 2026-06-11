-- Down: bypass_surface is pure derived data (rebuildable from entities + observations
-- via build_bypass_surface), so dropping it loses nothing — ADR-0001 §10 Q5, mirroring
-- 032_entity_embeddings.down.sql.
DROP TABLE IF EXISTS bypass_surface;
