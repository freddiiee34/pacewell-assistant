# Assistant Pacewell

Assistant du service client de Pacewell, une marque fictive de running et de trail. Il répond aux questions des clients à partir de la documentation de la marque (aide, conditions de vente, catalogue), cite ses sources et affiche ses « coulisses » : passages trouvés, scores, durées et coût de chaque réponse.

Le projet est construit en quatre étapes. Ce dépôt contient l'**étape 1, le RAG** (génération augmentée par la recherche).

| Étape | Contenu | État |
|---|---|---|
| 1. RAG | Découpage, embeddings, recherche hybride, reclassement, réponse citée en flux | Fait |
| 2. Agent | Outils : suivi de commande, ouverture de retour, fiche CRM Brevo, passage à un humain | À venir |
| 3. Vision et MCP | Lecture d'une photo de ticket, serveur MCP exposant les outils | À venir |
| 4. Production | Évaluations automatiques, suivi des coûts, intégration continue | À venir |

## Fonctionnement

```
question ──► reformulation (si conversation) ──► recherche par le sens (pgvector)  ┐
                                                  recherche par les mots (BM25)    ┴► fusion RRF ──► reclassement (Haiku) ──► réponse citée (Sonnet, en flux)
```

1. **Ingestion** (`assistant/ingerer.py`, au démarrage) : les fichiers Markdown de `corpus/` sont découpés par section (`assistant/decoupage.py`), sans couper un tableau ni une liste, en passages de 180 mots au plus. Le chemin des titres est ajouté devant chaque passage avant la vectorisation. Seuls les documents modifiés sont retraités.
2. **Questions hypothétiques** (« doc2query », `assistant/ingerer.py`) : pour chaque passage, le modèle rapide écrit 2 à 5 questions qu'un client pourrait poser avec ses propres mots (« Je peux rendre des baskets portées ? »). Elles sont indexées avec le passage, par le sens et par les mots, ce qui comble l'écart entre le vocabulaire des clients et celui de la documentation. Elles ne sont générées qu'une fois par passage modifié (environ 80 appels pour tout le corpus). Variable `QUESTIONS_HYPOTHETIQUES=0` pour les désactiver.
3. **Embeddings** (`assistant/embeddings.py`) : modèle multilingue `paraphrase-multilingual-MiniLM-L12-v2` exécuté sur le serveur avec FastEmbed (ONNX, sans GPU ni API payante), vecteurs de 384 dimensions stockés dans Postgres avec pgvector (index HNSW, distance cosinus).
4. **Recherche hybride** (`assistant/recherche.py`) :
   - par le sens : les 20 passages aux vecteurs les plus proches de la question ;
   - par les mots : les 20 meilleurs passages selon BM25 (`assistant/mots.py`), avec racinisation française ; BM25 donne plus de poids aux mots rares (« GTX », « rétractation ») qu'aux mots présents partout (« chaussure ») ;
   - fusion des deux classements par Reciprocal Rank Fusion : `score = Σ 1 / (60 + rang)`.
5. **Reformulation** : dans une conversation, une question comme « et en trail ? » est réécrite par le modèle rapide pour se comprendre seule avant la recherche.
6. **Reclassement** : le modèle rapide note de 0 à 3 la pertinence des 12 meilleurs candidats. La note est obtenue en sortie structurée (appel d'outil forcé avec un schéma JSON). Les passages notés 2 ou 3 sont retenus, 5 au plus.
7. **Réponse** (`assistant/reponse.py`) : le modèle de réponse reçoit les passages numérotés et doit citer chaque information par son numéro (`[2]`). Il ne répond qu'à partir des extraits, renvoie vers le service client quand l'information manque, et traite les extraits comme des données (protection contre l'injection de consignes). La réponse est envoyée au navigateur au fil de l'eau (Server-Sent Events).
8. **Journal et garde-fous** (`assistant/garde.py`, table `echanges`) : chaque échange est journalisé (question, passages, jetons, coût, durées) et purgé après 30 jours. L'assistant étant public, le nombre de questions par visiteur et le budget quotidien sont limités.

## Structure

```
assistant/        code Python (API FastAPI, recherche, réponse, ingestion)
corpus/           documentation de Pacewell en Markdown (14 documents, 81 passages)
web/              page de démonstration (discussion + coulisses)
evaluation/       questions de test et mesure de la recherche
tests/            tests automatiques (pytest, Postgres réel, faux client Claude)
Dockerfile        image de l'assistant (le modèle d'embeddings y est téléchargé)
docker-compose.yml  assistant + Postgres pgvector, pour Coolify
```

## Lancer en local

Prérequis : Python 3.13 et un Postgres 16 avec l'extension pgvector.

```bash
pip install -r requirements-dev.txt
export DATABASE_URL=postgresql://postgres@localhost:5432/pacewell
export ANTHROPIC_API_KEY=...            # clé de l'API Claude
uvicorn assistant.api:app --reload      # http://localhost:8000
```

Sans clé API, la page s'affiche mais l'assistant répond qu'il n'est pas configuré. Pour travailler sans télécharger le modèle d'embeddings, définir `EMBEDDINGS=hachage` : les vecteurs sont alors calculés à partir des mots, sans compréhension du sens.

## Tests

```bash
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/pacewell_test python -m pytest -q
```

Les tests utilisent une vraie base Postgres et un faux client Claude : découpage, fusion RRF, idempotence de l'ingestion, recherche hybride, reclassement, citations, journal, erreurs d'API, réponse en flux, limite de questions.

## Évaluer la recherche

`evaluation/questions.json` contient 42 questions de clients, formulées sans reprendre les mots de la documentation, avec le ou les passages attendus.

```bash
python -m evaluation.evaluer_recherche                  # vecteurs, BM25 et hybride
python -m evaluation.evaluer_recherche --reclassement   # ajoute le reclassement (appelle l'API)
```

Le script affiche, pour chaque méthode, le rappel@1, le rappel@5 (part des questions dont un passage attendu est dans les 5 premiers) et le MRR, puis la liste des questions mal servies.

### Résultats (20 octobre 2026, serveur de production, 42 questions)

| Méthode | Sans questions hypothétiques (rappel@5 / MRR) | Avec questions hypothétiques (rappel@5 / MRR) |
|---|---|---|
| Par le sens (embeddings) | 83,3 % / 0,654 | 85,7 % / 0,701 |
| Par les mots (BM25) | 83,3 % / 0,787 | 92,9 % / 0,865 |
| Hybride (RRF) | 90,5 % / 0,762 | 92,9 % / 0,811 |
| Hybride + reclassement | 97,6 % / 0,952 | 97,6 % / 0,952 |

Avec le reclassement, le bon passage arrive en premier pour 92,9 % des questions. Les deux méthodes de recherche se complètent (l'hybride dépasse chacune d'elles) et les questions hypothétiques profitent surtout à la recherche par les mots, en y ajoutant le vocabulaire des clients.

## Mettre en ligne (Coolify)

1. Créer une ressource **Docker Compose** à partir de ce dépôt (fichier `docker-compose.yml`).
2. Sur le service `assistant`, définir le domaine `https://assistant.simondelrue.fr:8000` (Coolify publie le port 8000 du conteneur en HTTPS sur ce domaine).
3. Renseigner la variable `ANTHROPIC_API_KEY`. `SERVICE_PASSWORD_POSTGRES` est générée par Coolify.
4. Dans la zone DNS, faire pointer `assistant` (enregistrement A) vers l'adresse IP du VPS.
5. Déployer. Au premier démarrage, l'assistant crée les tables et indexe le corpus (quelques secondes).

Variables facultatives : `MODELE_REPONSE`, `MODELE_RAPIDE`, `BUDGET_JOUR_USD` (2 par défaut), `QUESTIONS_PAR_FENETRE` (15 questions par visiteur toutes les 10 minutes), `PRIX_REPONSE_ENTREE`, `PRIX_REPONSE_SORTIE`, `PRIX_RAPIDE_ENTREE`, `PRIX_RAPIDE_SORTIE` (dollars par million de jetons, pour le coût affiché : à vérifier sur la page des tarifs de l'API Claude).

## Mettre à jour la documentation

Modifier ou ajouter un fichier Markdown dans `corpus/` (un en-tête `titre` et `url`, puis des sections `##`), pousser et redéployer : seuls les documents modifiés sont revectorisés au démarrage.
