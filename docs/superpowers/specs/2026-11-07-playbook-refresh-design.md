# Mise à jour du playbook d'orchestration — design

Date : 2026-11-07 · Demandeur : Radan · Statut : à valider

## Intention

`PLAYBOOK.md` décrit la façon de travailler de juin à août 2026 : Codex, GPT-5.6 Luna et Sol, Hermes, `pr-closure`.
Depuis, la pratique a changé, et le playbook induit les agents en erreur. Exemple du 07/11 : le capitaine ShopKartOS a
dû demander à une autre session (PB new) comment lancer un agent, parce que le playbook disait `codex exec`. Le but est
qu'un capitaine Claude Code qui lit le playbook travaille **exactement comme Radan travaille aujourd'hui**, sans poser
de questions.

## Constat (recherche du 07/11)

Sources : notes de session du vault des 30/10 au 06/11 (DigitalPrevention capitaine d9227481, PublyApp 05/11, ShopKartOS
01/11 et 06/11), registres `orchestration/ledger/`, `~/.dsh/patches/`, mémoire `model-routing-jcode-opencode-go`, et la
méthode décrite par la session PB new le 07/11.

- **Plus d'abonnement Codex depuis le 22/08.** Les exécutants passent par **OpenCode Go via `dsh-go`** : DeepSeek v4.1
  Flash code, Muse Spark 1.2 ou 1.3 relit. `jcode` / ox-alpha existe mais n'est plus la voie principale.
- **Rôle de Claude (règle Radan du 01/11)** : orchestrer, vérifier, commiter, pousser, ouvrir la PR, déployer. Claude ne
  code plus et ne relit plus, sauf pour la vision, Drive et Trello. Exception ponctuelle : une « fenêtre Claude » pour
  finir le quota hebdo (le 05/11 : Sonnet code, Opus relit).
- **Les agents `dsh-go` n'ont pas accès à `.git`** : ils laissent les changements non indexés et écrivent un rapport.
  Le capitaine commite.
- **Registre** : en pratique un simple `ledger.md` par travail, une ligne par lancement. Le JSONL détaillé n'est presque
  plus rempli depuis octobre.
- **`pr-closure` (§2.6, 15 états)** : lancé dans aucune session récente, dernière vraie utilisation en septembre (Hermes,
  PublyApp). Les configurations `.ai/project-closure-v1.json` existent encore dans PublyApp et DigitalPrevention.
- **Hermes** n'est presque plus utilisé depuis plusieurs semaines. Claude Code est le capitaine partout.
- **Radan ne lance presque plus de skills lui-même.** Il décrit ce qu'il veut et compte sur l'agent pour choisir le bon
  skill. Il ne se souvient que de deux ou trois des skills installés.

## Décisions de Radan

1. Le playbook s'adresse **directement à Claude Code** comme capitaine par défaut. Ce qui ne concerne que Hermes est
   retiré.
2. **§2.6 et `pr-closure` passent en annexe facultative.** Ils sont remplacés, dans le corps du playbook, par une courte
   checklist de fermeture : CI verte sur le commit poussé ; relecture par un autre modèle sur ce même commit ; aucune
   remarque bloquante ; feu vert de Radan pour le merge.
3. **Tableau « situation → skill »** : le playbook dit au capitaine quel skill utiliser selon ce que Radan demande.
   Radan n'a pas à s'en souvenir.
4. **Fichier d'adaptation par dépôt** : s'il manque, le capitaine **le crée lui-même au premier passage**, dans une
   version courte (commandes d'installation, de test et de vérification, pièges du dépôt), puis continue sans
   s'arrêter.
5. Le constat ci-dessus est juste (« non tu trompes pas »).

## Ce que devient le playbook

### Garder (principes de fond, réécrits pour Claude Code)

- Le capitaine orchestre et n'écrit pas le code.
- On ne commite et on ne pousse jamais sur la branche principale. On ne merge jamais sans le feu vert explicite de Radan,
  PR par PR.
- Toute tâche passe par une relecture faite par un modèle différent de celui qui a codé.
- Chaque agent reçoit un brief autonome, un seul capitaine par chantier, et on part d'un état propre et nommé.
- Les sous-agents ne touchent ni au vault ni à la mémoire : c'est le capitaine qui consigne.

### Mettre à jour

- **Modèles et lancement** : la commande exacte de PB new (`setsid -f … dsh-go --profile headless --patch
  ~/.dsh/patches/go-deepseek-v4.1-flash.yml - < prompt.txt > answer.md 2> err.log; echo exit=$? > done`), le prompt sur
  stdin depuis un fichier (jamais `"$(cat brief)"`), l'attente par une boucle en arrière-plan sur le fichier `done`
  (pas de sondage), et `exit=124` qui signifie un délai dépassé.
- **Qui fait quoi** : l'agent code, teste et écrit son rapport ; le capitaine relance lui-même les tests, lit le diff,
  commite, pousse, ouvre la PR et déploie.
- **Brief** (§3), aligné sur PB new : objectif avec la décision de Radan citée mot pour mot ; worktree et branche ;
  périmètre et ce qu'il ne faut pas toucher ; commandes de vérification exactes ; un bloc « Secrets » (ne jamais lire
  `.env*`) ; un bloc « Écritures » (pas de git, pas de vault ni de mémoire, pas de GitHub ou Trello, pas
  d'installation) ; un contrat de rapport (fichier de rapport créé sous 10 minutes et mis à jour à chaque étape ;
  réponse finale `STATUS DONE` ou `STATUS BLOCKED`) ; un délai réaliste (2 à 4 h pour une grosse tâche).
- **Registre** : un `ledger.md` par travail, une ligne par lancement (date, tâche, modèle de code, modèle de relecture).
  Le JSONL détaillé de §6 devient facultatif.
- **Pièges connus**, ajoutés :
  - un agent qui dépasse son délai sans rapport ;
  - un agent qui boucle (« fais-le UNE fois, sinon BLOCKED ») ;
  - un merge ou un rebase fait par l'agent sous forme de patch ;
  - des fichiers générés réécrits par le build ;
  - `pkill -f` ou un `pgrep` qui se trouve lui-même ;
  - `gh pr merge` lancé depuis un worktree qui ne fusionne rien ;
  - le dossier temporaire de la session effacé à la clôture (garder les livrables ailleurs) ;
  - un `rm` sur une variable non définie refusé (`"${VAR:?}"`).

### Ajouter

- **Le tableau « situation → skill »**, construit à partir des skills installés : `/reunion`, `/livre`, dp-merge-pr,
  dp-deploy-preprod, dp-preprod-qa, dp-proof-captures, dp-proof-word, dp-volet-*, brainstorming, writing-plans,
  systematic-debugging, second-opinion, archives-search, etc. Une ligne par situation que Radan décrit avec ses propres
  mots.
- **Le travail hors code** (vidéo, Word, captures) : le playbook renvoie aux skills qui portent ces pipelines, avec la
  règle commune des deux relectures hostiles par deux modèles différents et la vérification sur les images réelles.
- **La façon de travailler avec Radan** : répondre en français simple ; ne jamais demander la permission de lancer un
  agent (le feu vert ne concerne que le merge et le déploiement) ; vérifier avant d'annoncer « pas fait » ; donner une
  estimation de durée honnête ; fouiller les archives avant de demander.
- **La fenêtre Claude** : des sous-agents Claude seulement si Radan l'ouvre explicitement (finir le quota hebdo), avec
  toujours un modèle différent pour la relecture.

### Retirer ou alléger

- §2.6 et `pr-closure` → annexe facultative (décision 2). Les règles de fond de §2.6 qui restent valables sans l'outil
  (CI liée au commit exact, approbation invalidée par un nouveau commit, remarques bloquantes impossibles à reporter)
  passent dans la checklist de fermeture.
- La politique Luna / Sol, l'exception « même famille » et les profils Codex.
- Le chemin « mécanique / kanban » de §6 (Hermes).
- §5.4 et §5.5 : réduits à ce qui sert (context-mode, ponytail) ; Caveman, RTK et les mesures `codex debug` sont
  retirés.

## Contraintes

- Le playbook reste **court et lisible par un agent** : viser nettement moins que les 363 lignes actuelles.
- `PLAYBOOK.md` contient déjà une **modification non commitée** de Radan (§2.3 « garder la capacité occupée », §5.2
  `record-verification`) : elle doit être conservée ou intégrée, jamais écrasée.
- La branche locale `main` est **en avance de 2 commits** sur `origin` (`fix(closure)` du 28/09 et du 28/10). Ne rien
  pousser sans l'accord de Radan.
- Mettre à jour en même temps `captain-packet-template.md`, `adapter-template.md`, `README.md` et `CHANGELOG.md`
  (nouvelle version 0.3.0), puisqu'ils décrivent les mêmes lancements.
- Les fichiers `.ai/orchestration-adapter.md` de PublyApp et de DigitalPrevention ne sont pas réécrits dans ce chantier :
  ils seront mis à jour au prochain passage d'un capitaine, selon la décision 4.
- Ne rien supprimer dans `tools/` : `pr-closure` reste installé, l'annexe dit comment s'en servir.

## Hors périmètre

- Nettoyer les fichiers `.bak` de `tools/`.
- Réécrire `run-lane.sh` et `review-with-fallback.sh` (ancienne voie jcode / OpenRouter).
- Les CLAUDE.md globaux et des projets : à aligner ensuite, dans un travail séparé, quand le playbook sera validé.

## Échec, même si ça « marche »

- Un capitaine qui, après avoir lu le playbook, ne sait toujours pas lancer un agent `dsh-go`.
- Un capitaine qui s'arrête parce qu'un fichier d'adaptation manque.
- Un capitaine qui demande à Radan quel skill utiliser.
- La modification non commitée de Radan perdue.

## Précisions après relecture (Muse, 07/11) — mes choix sauf mention

1. **Commande de lancement complète** (méthode PB new, en usage le 07/11) :
   ```bash
   R=~/orchestration-runs/<AAAA-MM-JJ-sujet>   # dossier de travail durable, jamais le dossier temporaire de session
   echo "Read $R/<X>-brief.md first, then do it." > $R/<X>-prompt.txt
   setsid -f bash -c "eval \"\$(fnm env)\"; cd <worktree>; nice -n 10 ionice -c3 timeout <secondes> \
     dsh-go --profile headless --patch ~/.dsh/patches/<patch>.yml - < $R/<X>-prompt.txt \
     > $R/<X>-answer.md 2> $R/<X>-err.log; echo exit=\$? > $R/<X>-done"
   # attente : boucle en arrière-plan  until [ -f $R/<X>-done ]; do sleep 60; done
   ```
   Code : `go-deepseek-v4.1-flash.yml`, délai de 2 à 4 h. Relecture : `go-muse-spark-1.3-contributor.yml`, avec 1.2 en
   secours, délai d'environ 45 min. `exit=124` signifie un délai dépassé. Le capitaine crée la branche et le worktree
   **avant** le lancement, puisque l'agent n'a pas accès à git.
2. **La paire par défaut** : DeepSeek v4.1 Flash code, Muse Spark 1.3 relit. Elle vaut aussi pour les deux relectures
   hostiles du travail hors code (DeepSeek et Muse). La **fenêtre Claude** ne s'ouvre que si Radan le dit dans la
   session en cours (« utilise Claude », « finis le quota Claude »). Dans ce cas : Sonnet code, Opus relit.
3. **La limite du capitaine** : il peut lancer les tests et les vérifications, lire les diffs, faire tout le travail git
   (commit, push, PR, merge ou rebase, résolution de conflits) et restaurer les fichiers générés. Il ne modifie **jamais**
   le code produit, même pour une petite correction : elle repart vers un agent. Les exceptions vision, Drive et Trello
   couvrent les outils que Claude utilise lui-même (lecture d'images, connecteurs).
4. **La checklist de fermeture**, dans le corps du playbook, en 7 points :
   1. la CI GitHub est verte sur le commit poussé, si le dépôt en a une ;
   2. la vérification complète de l'adaptateur passe en local sur ce même commit ;
   3. la relecture par un autre modèle porte sur ce même commit et rend un `STATUS` avec la liste des remarques
      bloquantes ;
   4. aucune remarque bloquante ne reste ouverte ;
   5. un nouveau commit annule la relecture et la vérification ;
   6. une remarque bloquante ne se reporte jamais ;
   7. Radan donne son feu vert pour le merge.

   L'annexe `pr-closure` ne sert que si Radan la demande.
5. **L'adaptateur court** : même chemin `.ai/orchestration-adapter.md`, plus une ligne de renvoi dans `AGENTS.md`. Il
   contient seulement `default_branch`, `setup_cmd`, `test_cmd`, `acceptance_cmd`, `worktree_root` et `known_quirks`.
   Un adaptateur complet existant n'est pas réécrit : le capitaine corrige seulement les lignes de modèles et de
   lancement devenues fausses.
6. **L'inventaire des skills** : la référence, ce sont les skills réellement installés (`~/.claude/skills/` et les
   plugins listés par la session). Le rédacteur construit le tableau à partir de ces descriptions. Si aucun skill ne
   correspond, le capitaine travaille sans skill : il ne s'arrête pas et ne demande pas.
7. **Langue et longueur** (décision de Radan) : **anglais**. `PLAYBOOK.md` passe à environ 150 à 200 lignes. Les
   décisions de Radan sont citées en français, entre guillemets. `README.md` et les modèles sont raccourcis dans le même
   esprit.
8. **La livraison** : sur une branche `refresh-2026-11` du dépôt du playbook. La modification non commitée de Radan est
   d'abord commitée telle quelle, dans son propre commit. Ensuite viennent les commits de la mise à jour (version
   0.3.0). On ne pousse rien sans l'accord de Radan. La relecture finale est faite par Muse.

## Laissé au rédacteur

Le plan exact des sections, la formulation, l'ordre des lignes du tableau des skills, la forme de l'annexe.
