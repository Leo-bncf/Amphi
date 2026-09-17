# Amphi

Amphi est une application open source de prise de notes pour les cours en amphi.

Plusieurs appareils peuvent enregistrer la même séance. Les transcriptions sont ensuite fusionnées en une version canonique plus fiable, puis un LLM génère des notes structurées dont **chaque affirmation peut être reliée à un passage horodaté du cours**.

L'objectif est simple : produire des notes utiles sans perdre la possibilité de vérifier d'où vient l'information.

> 📐 **[ARCHITECTURE.md](./ARCHITECTURE.md)** — architecture, décisions techniques, coûts, risques et jalons.

---

## Télécharger Amphi

Si tu veux simplement utiliser l'application, **pas besoin de cloner le dépôt**.

Télécharge la dernière version depuis :

### **[GitHub Releases](../../releases/latest)**

Les builds disponibles sont publiés directement dans l'onglet **Releases**.

> Le développement se concentre actuellement principalement sur macOS.

---

## Fonctionnement

```text
Enregistrements
      ↓
Transcriptions
      ↓
Fusion / consensus
      ↓
Transcription canonique
      ↓
Génération des notes
      ↓
Validation des sources
      ↓
Notes structurées + timestamps
