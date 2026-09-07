# Changelog

## [M1] — en cours

### Ajouté
- Socle du monorepo : pnpm workspaces, Turborepo, TypeScript strict partagé.
- `packages/shared` — identifiants typés (branded), modèle d'horloge de session, schémas zod du domaine, interfaces `AsrProvider` et `LlmProvider`, schéma du document de notes avec ancrage obligatoire.
- `bench/` — banc de mesure du débit Whisper sur Apple Silicon, avec calcul de WER et projection mensuelle. Texte de cours de référence en français avec jargon anglais, réutilisable comme fixture pour le banc de consensus.

## [M0] — 2026-09-08

### Ajouté
- `ARCHITECTURE.md` : flux, décisions (17 ADR), modèle de données, coûts vérifiés, risques, jalons.

### Décisions structurantes
- Auto-hébergement sur `infra-pve-2` derrière un tunnel Cloudflare dédié — la ligne est en CGNAT, aucun port-forwarding n'est possible.
- Worker ASR local sur le Mac M4, repli payant avec dégradation de `K` — ce qui rend la fusion multi-appareils gratuite et donc activable par défaut.
- Mistral Small 4 pour le document final : sous Haiku 4.5, les fournisseurs se tiennent en 22 centimes par mois, le prix cesse d'être un critère.
- `MediaRecorder` écarté au profit d'AudioWorklet + Opus WASM : iOS ne produit pas d'Opus et ses chunks ne sont pas décodables isolément.
- Modèle d'horloge affine (pente + décalage) avec ré-ancrage continu : la dérive des quartz domine l'offset initial et atteint 360 ms sur une heure.
- Liste d'invités plutôt qu'allowlist de domaine pour l'authentification.

### Chiffres retenus
- Budget nominal **2,77 €/mois**, pire cas 7,14 €/mois, pour ~130 h de cours par mois.
- Coût d'une heure de cours : 0,005 à 0,047 € — le critère du brief était < 0,30 €.
