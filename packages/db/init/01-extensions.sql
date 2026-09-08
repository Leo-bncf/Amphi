-- pgvector doit exister avant la première migration : la table `embeddings`
-- déclare une colonne de type vector et un index HNSW.
CREATE EXTENSION IF NOT EXISTS vector;

-- Recherche plein texte française (§3.8), utilisée à côté de la recherche
-- sémantique : les deux se complètent, l'une trouve les mots exacts, l'autre les
-- reformulations.
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
