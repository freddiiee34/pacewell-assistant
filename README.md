# Assistant Pacewell

Assistant du service client de Pacewell, une marque fictive de running et de trail. C'est un agent : il répond aux questions à partir de la documentation de la marque en citant ses sources, consulte une commande, ouvre un retour ou un échange, lit la photo d'un bon de livraison et passe la main à un conseiller quand il le faut. Chaque réponse affiche ses « coulisses » : outils appelés, passages trouvés, durées, jetons et coût.

Démonstration : https://assistant.simondelrue.fr · tableau de bord : https://assistant.simondelrue.fr/tableau-de-bord.html

| Étape | Contenu | État |
|---|---|---|
| 1. RAG | Découpage, embeddings, recherche hybride, questions hypothétiques, reclassement, réponse citée en flux | Fait |
| 2. Agent | Boucle d'outils : documentation, suivi de commande, ouverture de retour, passage à un humain (n8n) | Fait |
| 3. Vision et MCP | Lecture d'une photo de bon de livraison, serveur MCP exposant les outils à Claude | Fait |
| 4. Production | Cache de prompt, suivi des coûts, tableau de bord, évaluations automatiques, intégration continue | Fait |

## Étape 1 : la recherche dans la documentation (RAG)

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
7. **Réponse** : les passages retenus sont renvoyés à l'agent, numérotés, et il doit citer chaque information par son numéro (`[2]`). Il ne répond qu'à partir des extraits, renvoie vers le service client quand l'information manque, et traite les extraits comme des données (protection contre l'injection de consignes). La réponse est envoyée au navigateur au fil de l'eau (Server-Sent Events).
8. **Journal et garde-fous** (`assistant/garde.py`, table `echanges`) : chaque échange est journalisé (question, passages, jetons, coût, durées) et purgé après 30 jours. L'assistant étant public, le nombre de questions par visiteur et le budget quotidien sont limités.

## Étape 2 : l'agent et ses outils

L'agent (`assistant/agent.py`) reçoit la question, l'historique de la conversation et la date du jour. Le modèle de réponse décide lui-même quels outils appeler ; leurs résultats lui sont renvoyés et il continue, jusqu'à 6 tours, avant d'écrire la réponse finale en flux. Une phrase écrite avant un appel d'outil (« Je vérifie la commande. ») est affichée comme une étape d'attente puis retirée : seule la réponse finale reste dans la conversation et dans le journal.

| Outil | Rôle |
|---|---|
| `chercher_documentation` | lance la recherche de l'étape 1 et renvoie les passages numérotés |
| `consulter_commande` | statut, transporteur, articles et dates d'une commande (`assistant/boutique.py`) |
| `ouvrir_retour` | vérifie l'éligibilité d'un retour ou d'un échange, puis l'enregistre une fois confirmé |
| `transmettre_a_un_humain` | crée une demande `SAV-…` et prévient le service client par un webhook n8n (email Brevo) |

La boutique de démonstration contient 5 commandes (`donnees/commandes.json`), dont les dates sont recalculées à chaque démarrage pour que les cas restent valables : une commande livrée récemment, une en transit, une hors délai de retour, etc. Les commandes de test sont affichées sur la page de démonstration.

### Sécurité, appliquée dans le code et non dans le prompt

Le prompt demande à l'agent d'être prudent, mais les règles qui protègent les données sont vérifiées par les outils eux-mêmes :

- une commande n'est lue que si le numéro **et** l'email correspondent ; le message de refus est le même, que la commande existe ou non (impossible de deviner des numéros valides) ;
- un retour n'est enregistré qu'avec `confirme=true` : l'agent annonce d'abord les conditions, le client confirme au message suivant ;
- les règles de retour (commande livrée, 30 jours, chaussettes et nutrition non reprises, pas de doublon) sont codées, pas laissées au modèle ;
- les emails sont masqués (`c***@exemple.fr`) dans les coulisses et le journal ;
- les résultats d'outils et les documents joints sont présentés comme des données, jamais comme des consignes.

## Étape 3 : vision et serveur MCP

### Lecture d'un document

Le client peut joindre la photo d'un bon de livraison ou d'un ticket (`assistant/documents.py`). Le navigateur la redimensionne (1 600 px, JPEG), le serveur vérifie le type et la taille (5 Mo au plus), puis le modèle rapide la lit et remplit un schéma strict par appel d'outil forcé : type de document, marque, numéro de commande, date, articles, total, lisibilité. Seul ce JSON est transmis à l'agent, pas l'image : la réponse coûte moins cher et une consigne écrite sur la photo n'est jamais lue comme une instruction. Un exemple est fourni (`web/exemples/bon-de-livraison.jpg`, généré par `scripts/generer_exemple.py`).

### Serveur MCP

Les outils sont aussi exposés en MCP (Model Context Protocol) sur `https://assistant.simondelrue.fr/mcp` (`assistant/serveur_mcp.py`, transport HTTP « streamable », sans état). N'importe quel client MCP peut s'en servir, par exemple Claude :

```bash
claude mcp add --transport http pacewell https://assistant.simondelrue.fr/mcp
```

Trois outils sont publiés : `chercher_documentation`, `consulter_commande` et `ouvrir_retour`, avec les mêmes contrôles (numéro + email, confirmation) et le même budget quotidien. Les appels sont journalisés avec le canal `mcp`. La page `/mcp.html` explique l'ajout dans Claude.

## Étape 4 : production

- **Cache de prompt** : la consigne système et la définition des outils sont marquées `cache_control`. À partir du deuxième tour d'une même réponse, elles sont relues depuis le cache (facturé 10 % du prix normal).
- **Coût de chaque réponse** : jetons d'entrée, de sortie, d'écriture et de lecture du cache, pour chaque modèle, convertis en dollars. Les durées sont mesurées par étape (lecture du document, premier mot, modèle, outils, total).
- **Journal** (table `echanges`) : outils appelés, canal (web, MCP, évaluation), coût et durées. Purgé après 30 jours.
- **Tableau de bord** (`/tableau-de-bord.html`, API `/api/statistiques`) : échanges et coût par jour, coût moyen, temps de réponse (médiane et 95e centile), part du cache, usage des outils, et résultats de la dernière évaluation. Il n'affiche que des agrégats : aucune question de visiteur n'est exposée.
- **Garde-fous** : questions limitées par visiteur, budget quotidien, taille des images et de l'historique bornées.
- **Intégration continue** (`.github/workflows/tests.yml`) : à chaque push, GitHub Actions démarre un Postgres pgvector et lance les tests.

## Structure

```
assistant/          code Python : API FastAPI, agent, outils, boutique, recherche, vision, serveur MCP
corpus/             documentation de Pacewell en Markdown (14 documents, 81 passages)
donnees/            commandes de démonstration
web/                page de démonstration, tableau de bord, page MCP
evaluation/         questions et scénarios de test, mesure de la recherche et de l'agent
scripts/            génération de l'exemple de bon de livraison
tests/              tests automatiques (pytest, Postgres réel, faux client Claude)
.github/workflows/  intégration continue
Dockerfile          image de l'assistant (le modèle d'embeddings y est téléchargé)
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

Les tests utilisent une vraie base Postgres et un faux client Claude : découpage, fusion RRF, ingestion, recherche hybride, reclassement, boucle d'outils de l'agent, contrôles de sécurité de la boutique (email, confirmation, délai), retour en deux temps, lecture de document, validation des images, serveur MCP (initialisation, liste et appel d'outils), statistiques, erreurs d'API, limite de questions.

## Évaluer

### La recherche

`evaluation/questions.json` contient 42 questions de clients, formulées sans reprendre les mots de la documentation, avec le ou les passages attendus.

```bash
python -m evaluation.evaluer_recherche                  # vecteurs, BM25 et hybride
python -m evaluation.evaluer_recherche --reclassement   # ajoute le reclassement (appelle l'API)
```

Le script affiche, pour chaque méthode, le rappel@1, le rappel@5 (part des questions dont un passage attendu est dans les 5 premiers) et le MRR, puis la liste des questions mal servies.

### L'agent

`evaluation/scenarios.json` contient 14 conversations de test : questions de documentation, suivi de commande, tentatives d'accès sans le bon email, injection de consigne, demande hors sujet, retours refusés, échange en deux temps, passage à un humain, photo de bon de livraison. Chacune est jouée pour de vrai, puis contrôlée de deux façons :

- **contrôles automatiques** : outils attendus ou interdits, faits présents dans la réponse, informations qui ne doivent pas fuiter, retour ouvert ou non, citations ;
- **IA juge** : le modèle rapide relit la réponse avec les résultats des outils et signale toute affirmation qui n'y figure pas (fidélité).

```bash
python -m evaluation.evaluer_agent     # environ 14 conversations, quelques centimes
python -m evaluation.evaluer_tout      # recherche + agent, résultat publié sur le tableau de bord
```

### Résultats de la recherche (20 octobre 2026, serveur de production, 42 questions)

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
3. Renseigner la variable `ANTHROPIC_API_KEY`. `SERVICE_PASSWORD_POSTGRES` est générée par Coolify. `N8N_ESCALADE_URL` indique le webhook n8n appelé lors d'un passage à un humain.
4. Dans la zone DNS, faire pointer `assistant` (enregistrement A) vers l'adresse IP du VPS.
5. Déployer. Au premier démarrage, l'assistant crée les tables, charge les commandes de démonstration et indexe le corpus.
6. Pour publier une évaluation sur le tableau de bord, lancer `python -m evaluation.evaluer_tout` dans le terminal du conteneur.

Variables facultatives : `MODELE_REPONSE`, `MODELE_RAPIDE`, `BUDGET_JOUR_USD` (2 par défaut), `QUESTIONS_PAR_FENETRE` (15 questions par visiteur toutes les 10 minutes), `PRIX_REPONSE_ENTREE`, `PRIX_REPONSE_SORTIE`, `PRIX_RAPIDE_ENTREE`, `PRIX_RAPIDE_SORTIE` (dollars par million de jetons, pour le coût affiché : à vérifier sur la page des tarifs de l'API Claude).

## Mettre à jour la documentation

Modifier ou ajouter un fichier Markdown dans `corpus/` (un en-tête `titre` et `url`, puis des sections `##`), pousser et redéployer : seuls les documents modifiés sont revectorisés au démarrage.
