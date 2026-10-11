# Relance Garage — pré-production

Système de relance automatisée des factures impayées, pour garages
automobiles indépendants. Il fonctionne localement et gratuitement par défaut ;
la rédaction Anthropic et l’envoi par messagerie sont des intégrations facultatives.

Tout ce que voit le client ou le garagiste est en français. Le code, les
commentaires et les noms de variables sont en anglais.

## État de cette version

### Clients et paiements manuels

Voir [le parcours de démonstration personnalisable](MANUAL-WORKFLOWS-REVIEW.md).

- **Clients → Ajouter un client** : nom, e-mail, téléphone, adresse et contact.
  La fiche client permet ensuite de modifier les coordonnées ou d’ajouter une facture.
- **Factures → Ajouter une facture** : suivi d’une facture existante (pas création
  d’un document comptable). Les montants TTC, dates et client sont saisis explicitement.
- **Fiche facture → Marquer comme payée**, ou **Paiement partiel ou correction** :
  payé, impayé, total déjà reçu, date et note interne. Historique conservé.
- Un paiement manuel retire les anciens messages non envoyés. Les envois déjà
  effectués restent dans l’historique. Un import contradictoire est signalé,
  jamais appliqué silencieusement ; corriger le paiement sur la fiche si nécessaire.
- En démonstration, **Aperçu → Tester cette relance dans mon Gmail** reprend le
  message validé et adresse la copie exclusivement au compte connecté. Aucun envoi.
- Les saisies restent dans le contexte choisi et survivent au redémarrage.
  Réinitialiser la démonstration supprime aussi ses ajouts manuels.
- Tests : `python scripts/check_manual_workflows.py` (27 tests isolés, envois bloqués).

Cette application est **prête pour une démonstration, pas pour la production**.

Complément du 5 septembre 2026 : voir [la revue UX et le test Gmail](DEMO-REVIEW-2026-09-05.md).
Le parcours **Paramètres → Préparer le test Gmail** ouvre un message fictif
adressé uniquement au compte connecté. Gmail le conserve en brouillon ; aucun
envoi n’est effectué par ce test. La connexion au site et la session Gmail du
navigateur sont distinctes. Un aperçu ne compte jamais comme un envoi, même
avec les données de démonstration.

* chaque démarrage revient en **Mode démonstration** ; un ancien cookie « réel »
  n’est pas réutilisé après redémarrage ;
* le mode courant est écrit sur chaque page et le sélecteur
  **Démonstration / Données réelles** ne montre jamais les noms de fichiers ;
* un bouton resté ouvert dans un ancien onglet ne peut pas agir sur l’autre
  contexte de données ;
* une remise à zéro reconstruit atomiquement la démonstration et refuse de
  démarrer si les deux contextes pointent vers le même emplacement ;
* `DEMO_MODE=1` garde également les données réelles en aperçu de pré-production :
  aucune relance réelle n’est enregistrée comme envoyée ;
* les contrôles automatisés sont `python scripts/check_logic.py` (métier) et
  `python scripts/check_product_safety.py` (séparation, concurrence, import,
  transitions et absence d’envoi externe), ainsi que
  `python scripts/check_gmail_demo.py` (brouillon fictif, confidentialité et
  aperçus sans faux envois).

Avant production, il reste notamment à ajouter l’authentification si
l’application quitte la boucle locale, déplacer/rotater les identifiants déjà
créés et fournir un écran encadré pour confirmer les conditions commerciales
des clients importés.

## Règles du produit

1. **Le modèle de langage ne calcule jamais un montant.** Totaux, jours de
   retard, pénalités : tout est calculé en Python, en centimes entiers. Le
   modèle rédige uniquement le texte du message.
   `python scripts/check_logic.py` le prouve, contrôle par contrôle.
2. **Rien n'est envoyé automatiquement.** Chaque message passe par la file de
   validation du tableau de bord. En mode démo, aucun envoi réel : aperçu
   uniquement.
3. **Ton professionnel et non agressif**, adapté à une relation commerciale de
   proximité avec des clients réguliers.

## Démarrage

Double-cliquer sur **`start.bat`**. Il détecte Python, crée l'environnement
virtuel, installe les dépendances, génère la base si besoin et lance
l'application. Rien d'autre à faire.

En ligne de commande :

```bash
export PATH="/c/Users/samzy/AppData/Local/Programs/Python/Python312:$PATH"
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
```

Python 3.12 est installé via winget mais absent du PATH : d'où l'export
ci-dessus, que `start.bat` fait tout seul.

## Jeu de démonstration

```bash
python -m app.seed --check
```

Crée `data/garage.db` : 24 clients, 27 véhicules, **70 factures** dont 42
réglées (60 %). Le générateur est déterministe : deux démos racontent la même
histoire, mais les dates restent relatives à aujourd'hui, donc le jeu ne périme
jamais.

Chaque facture tire ses montants de son propre générateur, indexé sur le client
et la prestation : retoucher une facture ne rebat pas les cartes des 69 autres.

Le livre est composé de deux blocs :

* **l'encours** — 28 factures placées à la main, réparties sur les 4 niveaux
  d'escalade, plus 3 non échues et 2 juste sous le premier seuil ;
* **l'historique** — 30 factures réglées sur les 6 derniers mois, générées,
  pour que le livre ressemble à celui d'un garage qui tourne et pour alimenter
  les compteurs de l'étape 4.

`--check` vérifie : HT + TVA = TTC, total des lignes = total de la facture,
montants dans la fourchette 80 € – 2 400 €, statuts et acomptes cohérents,
aucun règlement antérieur à sa facture, plaques uniques, part de factures
réglées autour de 60 %.

## Escalade

| Niveau | Seuil | Intention | Canal |
| --- | --- | --- | --- |
| 1 | J+3 | On suppose un simple oubli | E-mail |
| 2 | J+10 | Ton plus ferme, rappel des conditions | E-mail |
| 3 | J+21 | Dernier avis avant recouvrement | E-mail |
| 4 | J+35 | Mise en demeure, à imprimer | Courrier recommandé AR |

Règles appliquées par `app/overdue.py` :

* une facture échue depuis moins de 3 jours reste **sous surveillance** :
  aucune relance ;
* un niveau déjà traité ne repasse pas. Valider, envoyer **ou ignorer** ferme
  le niveau — si le garagiste a dit non, le système n'insiste pas ;
* une facture qui franchit un palier déclenche un nouveau brouillon au niveau
  atteint, sans rattraper les niveaux sautés ;
* les pénalités portent sur le **solde dû**, acompte déduit, jamais sur le
  total facturé ;
* pénalités de retard à partir du niveau 2 seulement — au niveau 1 l'échange
  reste commercial ;
* indemnité forfaitaire de recouvrement de 40 € pour les **clients
  professionnels uniquement**, jamais pour un particulier.

Les pénalités dépendent des conditions propres à chaque client : voir la
section **Conditions de pénalité** ci-dessous, à lire avant toute mise en
service réelle.

## Conditions de pénalité — à confirmer avant tout usage réel

**Il n'existe pas de taux global dans ce système, et c'est délibéré.**

Les pénalités de retard ne relèvent pas du logiciel : elles relèvent du contrat
passé avec chaque client. Chaque fiche client porte donc son propre taux, sa
propre référence de conditions générales, et un indicateur de confirmation.

### Ce qui diffère entre un professionnel et un particulier

| | Client professionnel | Client particulier |
| --- | --- | --- |
| Pénalités de retard | Prévues par les CGV acceptées entre professionnels ; un taux d'au moins trois fois le taux d'intérêt légal est la clause usuelle | Exigibles uniquement si une clause a été acceptée, par exemple sur l'ordre de réparation signé ; le taux pratiqué est généralement plus faible |
| Indemnité forfaitaire de recouvrement (40 €) | Applicable — prévue par l'article L441-10 du code de commerce entre professionnels | **Jamais applicable** |
| Référence contractuelle | CGV professionnelles, contrat-cadre | Conditions de réparation, devis ou ordre de réparation signé |

Ces deux régimes ne sont pas interchangeables. Appliquer à un particulier un
taux et une indemnité prévus pour un professionnel expose le garage bien plus
qu'un impayé.

### Ce que fait le système tant que rien n'est confirmé

Tant que `penalty_terms_confirmed` n'est pas positionné pour un client, le
système **ne réclame aucune pénalité** sur ses factures. Il ne se rabat pas sur
une valeur par défaut : un taux que personne n'a validé n'a rien à faire sur une
facture. Les factures concernées sont signalées dans l'encours :

```
⚠  2 facture(s) échue(s) sans conditions de pénalité confirmées :
   aucune pénalité n'est réclamée dessus.
```

L'indemnité forfaitaire de 40 € fait exception : elle est fixée par la loi et
non par le contrat, elle s'applique donc aux clients professionnels
indépendamment de la confirmation du taux.

### Un type de client deviné ne réclame rien

L'indemnité forfaitaire de 40 € est prévue par la loi **entre professionnels**.
À l'import, le type particulier/professionnel n'est que **deviné d'après le
nom** : « Menuiserie Martin » est peut-être M. Martin lui-même.

Un type deviné sert donc à une seule chose : proposer un délai de paiement. Il
ne déclenche **aucune indemnité**. Comme pour le taux de pénalité, il faut une
confirmation humaine.

Ces clients apparaissent sur le tableau de bord au même endroit que ceux dont
les CGV ne sont pas confirmées, avec leur propre libellé : le garagiste règle
les deux en une seule fois.


### Avant la première utilisation réelle

Le garagiste doit, pour chaque client :

1. retrouver les conditions effectivement acceptées (CGV signées, ordre de
   réparation, devis) ;
2. renseigner le taux correspondant dans `late_penalty_rate_bp` et sa référence
   dans `cgv_reference` ;
3. ne passer `penalty_terms_confirmed` à 1 qu'une fois cette vérification faite.

Les valeurs de `DEFAULT_PENALTY_TERMS` dans `app/config.py` ne sont qu'un point
de départ proposé à la création d'une fiche client. **Elles ne valent pas
validation juridique** et doivent être revues avec les conditions réelles du
garage. En cas de doute sur la rédaction d'une clause pénale ou sur une mise en
demeure, faire relire par un professionnel du droit.

## Rédaction des messages

Deux moteurs, même garanties :

| Moteur | `LLM_MODE` | Coût | Usage |
| --- | --- | --- | --- |
| Messages pré-rédigés | `mock` *(défaut)* | gratuit, hors ligne | démonstration, développement |
| API Anthropic | `anthropic` | facturé à l'appel | production |

### Ce que reçoit le moteur de rédaction

Uniquement des **chaînes déjà formatées** : montants en euros déjà mis en forme,
dates déjà écrites à la française, retard déjà formulé. Aucun nombre brut, donc
rien à additionner, comparer ou convertir. Le modèle écrit de la prose autour de
chiffres décidés par `app/overdue.py`.

```bash
python scripts/draft_followups.py --prompt FA-2026-1202
```

affiche exactement ce qui partirait à l'API, sans consommer un crédit.

### Le contrôle des chiffres

Après rédaction, **tout nombre présent dans le message est recherché dans les
données transmises au modèle**. Un seul chiffre absent et le message est
refusé : il n'entre pas dans la file de validation, il est conservé avec son
motif et signalé au garagiste.

Les écritures équivalentes sont reconnues : `2 153,04`, `2 153,04` (espace fine),
`2153,04` et `2153.04` sont le même nombre.

```bash
python scripts/draft_followups.py --demo-guardrail
```

fait tourner la même facture dans le moteur normal puis dans un moteur qui
invente des chiffres, et montre le blocage. Utilisable tel quel en rendez-vous.

Ce que le contrôle garantit : la **provenance** de chaque chiffre. Ce qu'il ne
garantit pas : qu'un chiffre correct soit employé au bon endroit. D'où la
validation humaine, qui reste obligatoire dans tous les cas.


## Tableau de bord

`start.bat` prépare les relances du jour et ouvre la page sur
`http://127.0.0.1:8000`. L’interface reste en HTML sans framework côté client,
mais elle est désormais organisée en six espaces métier : **Tableau de bord,
Factures, Clients, Import, Relances, Paramètres**.

Dans l'ordre de lecture :

1. le contexte visible — données fictives ou données réelles ;
2. **Reste à encaisser**, avec la part échue et la part à venir ;
3. une seule prochaine action : vérifier ou préparer les relances ;
4. trois repères secondaires : retard le plus ancien, relances envoyées et
   encaissé après relance.

### Les trois actions

* **Valider** — le message est approuvé et passe dans « Prêtes à envoyer ».
  En démonstration, le bouton suivant affiche un aperçu sans ouvrir de réseau.
* **Modifier** — le texte devient éditable, objet compris. Enregistrer valide la
  version modifiée. Le contrôle des chiffres est relancé mais **ne bloque pas** :
  le garagiste est maître de ses mots. S'il introduit un chiffre absent de la
  facture, il en est averti et décide.
* **Ignorer** — le niveau est clos. Une action d’annulation reste disponible
  tant que le message n’a pas été envoyé.

La remise à zéro se trouve dans **Paramètres** lorsque le contexte Démonstration
est actif. Les réglages et comptes réels y sont entièrement masqués.

### Compteurs sur les 30 derniers jours

La fenêtre glisse : elle va toujours d'aujourd'hui aux 29 jours précédents,
bornes comprises. Ce n'est pas un détail d'affichage. Un mois calendaire remet
ces trois chiffres à zéro à minuit le 1er : le garagiste qui ouvre son tableau
de bord le 3 septembre lirait « 0 relance envoyée · 0,00 € encaissé » quelques
jours après que l'outil lui a récupéré trois mille euros, et il en conclurait
que le produit est mort. Ces compteurs ne sont pas de la comptabilité, ce sont
la preuve que l'outil travaille — cette preuve ne doit pas disparaître à cause
du calendrier.

Les définitions, elles, ne changent pas :

* *relances envoyées* : messages effectivement partis après validation ;
* *encaissé après relance* : factures réglées dans la fenêtre **pour lesquelles
  une relance était partie avant le règlement**. Les règlements spontanés ne sont
  pas comptés — le chiffre serait plus flatteur mais malhonnête ;
* *temps de rédaction économisé* : relances préparées × 12 min. Les messages
  bloqués par le contrôle ne comptent pas : ils n'ont rien fait gagner.

Le tableau de bord affiche les dates exactes de la fenêtre sous le titre, pour
qu'aucun chiffre ne soit sans période.


## Faire la démonstration

### Avant d'ouvrir la visio

Double-cliquer sur **`reset-demo.bat`**, ou utiliser
**Paramètres → Réinitialiser la démonstration**. Cette action ne connaît que
l’emplacement de la démonstration et vérifie que l’emplacement réel est distinct.

Puis **`start.bat`**. La page s'ouvre seule.

### Le parcours

1. vérifier que **Mode démonstration** est visible ;
2. ouvrir **Factures**, puis une facture en retard ;
3. ouvrir **Relances**, vérifier un message et le valider ;
4. afficher l’aperçu sécurisé ;
5. montrer **Paramètres** : les comptes réels sont masqués et la remise à zéro
   annonce explicitement qu’elle ne touche pas aux données réelles.

### Les trois objections qui viennent

**« C'est l'IA qui calcule mes montants ? »**
Non. Ouvrir un terminal et lancer `python scripts/check_logic.py` : plus de 600
contrôles, dont l'arithmétique au centime et le contrôle des chiffres inventés.
Le modèle ne reçoit que du texte déjà formaté — `scripts/draft_followups.py
--prompt FA-2026-1202` montre exactement ce qu'il voit.

**« Ça va envoyer des trucs à mes clients sans que je voie ? »**
Rien ne part sans un clic. Le bandeau « Démo · aucun envoi réel » est en haut de
l'écran, et en démonstration aucun envoi n'est même possible.

**« Et si je veux juste laisser tomber une facture ? »**
Bouton **Ignorer**. Le palier est clos, le système ne le repropose jamais. Et
s'il change d'avis : **Remettre en attente**.

### Ce qu'il ne faut pas promettre

Le contrôle des chiffres garantit qu'aucun montant n'est inventé. Il ne
garantit pas qu'un montant juste soit employé au bon endroit — d'où la
validation humaine. Le dire soi-même : un garagiste à qui on a promis zéro
risque cherche l'erreur pendant six mois.

Et avant toute mise en service réelle : reprendre la section
**Conditions de pénalité** avec le garagiste, dossier par dossier.


## Import des factures réelles — lecture des fichiers

> Étape 6, partie 1. Le récapitulatif avant import et le bouton
> « ouvrir dans ma messagerie » arrivent ensuite.

Le système lit le fichier du garage tel qu'il sort de son logiciel. Aucun
format n'est imposé : ni séparateur, ni encodage, ni ordre des colonnes.

```bash
python scripts/make_samples.py                                  # crée samples/
python scripts/inspect_file.py samples/export-logiciel-A.csv
python scripts/inspect_file.py samples/export-logiciel-A.csv --parse
```

`inspect_file.py` lit et affiche, **sans rien écrire nulle part**.

### Ce qui est détecté, jamais supposé

| | Détection |
| --- | --- |
| Séparateur | `;` `,` tabulation `\|` — choisi en mesurant lequel donne un tableau cohérent, pas par convention |
| Encodage | BOM UTF-8, UTF-16, UTF-8, Windows-1252, repli Latin-1 qui ne plante jamais |
| Format | `.csv`, `.txt`, `.tsv`, `.xlsx`, `.xlsm` |
| En-têtes | doublons et colonnes sans titre renommés pour rester lisibles |

Le repli Latin-1 est délibéré : mieux vaut un accent douteux et un avertissement
qu'un plantage sur le fichier du client pendant un rendez-vous.

### Montants — jamais un flottant

Un montant va de ses chiffres aux centimes entiers, par manipulation de chaînes
et arithmétique entière. Toutes ces écritures donnent 123 456 centimes :

```
1 234,56      1 234,56      1 234,56      1234.56
1.234,56      1,234.56      1 234,56 €    1 234,56 EUR
```

(les espaces peuvent être ordinaires, insécables ou fines)

Sont aussi compris : `-1 234,56`, `(1 234,56)` et `1 234,56-` — les trois façons
dont un logiciel comptable écrit un avoir.

**Pourquoi pas un flottant.** Sur les 4 000 premières valeurs en centimes, 272
sont corrompues par un aller-retour en virgule flottante : `1,15 €` devient 114
centimes, `0,29 €` devient 28. Le contrôle `check_logic.py` vérifie les 4 000.

### Ce qui est signalé plutôt que décidé en silence

| Cas | Lecture retenue | Signalé |
| --- | --- | --- |
| Plus de deux décimales | arrondi au centime le plus proche | oui |
| `12/31/2026` | mois/jour/année | oui |
| `46022` dans une colonne de dates | numéro de série Excel | oui |

Un groupement incohérent (`12,34,56`, `1.23.456`) n'est **pas** interprété : il
est refusé. Un montant mal formé sur une mise en demeure coûte plus cher qu'une
ligne d'import en moins.

### La convention des séparateurs se décide par colonne

Un montant isolé peut être illisible alors que sa colonne est parfaitement
claire. `1,234` vaut soit 1 234 €, soit 1,23 €. Le système ne tranche jamais
valeur par valeur : il lit **toute la colonne**, en déduit la convention du
fichier, puis relit chaque valeur avec cette seule règle.

| La colonne contient | Conclusion | `1,234` y vaut |
| --- | --- | --- |
| `189,90` — une virgule suivie de 2 chiffres | la virgule est décimale | **1,23 €** |
| `189.90` — le point est décimal | la virgule groupe les milliers | **1 234,00 €** |
| rien qui tranche | aucune convention | **ligne écartée** |

Quand la colonne ne tranche pas, **rien n'est deviné** : la ligne est refusée et
nommée dans le récapitulatif.

**Pourquoi ne jamais deviner.** L'erreur n'est pas symétrique. Lire `1,234`
comme 1 234 € alors qu'il s'agissait de 1,23 € envoie une mise en demeure mille
fois trop élevée : le garage perd le client et se met dans son tort. L'erreur
inverse ne fait que sous-réclamer, et le garagiste s'en aperçoit. Une ligne
écartée coûte moins cher qu'un chiffre faux.

Les trois fichiers `samples/convention-*.csv` couvrent exactement ces trois cas
et sont vérifiés par `check_logic.py`.


### Dates comprises

`31/12/2026` · `2026-12-31` · `31-12-2026` · `31.12.2026` · `31/12/26` ·
`20261231` · `46022` (série Excel) · cellules de date natives d'Excel.

L'ordre jour/mois est le français par défaut. Le format américain n'est retenu
que lorsqu'aucune autre lecture n'est possible, et il est alors signalé.

### Fichiers d'exemple

`samples/` contient trois exports volontairement pénibles, un par « logiciel » :

* **convention-decimale / -milliers / -indecidable** — les trois cas de la
  règle de convention ci-dessus ;
* **A** — point-virgule, Windows-1252, dates `jj/mm/aaaa`, montants avec `€` et
  espaces insécables, un doublon de numéro, un client sans e-mail, une facture
  déjà réglée, un montant illisible et un montant vide ;
* **B** — virgule, UTF-8 avec BOM, dates ISO, HT + TVA + TTC séparés ;
* **C** — `.xlsx`, dates en numéros de série Excel, nombres natifs.

Ils servent aux contrôles automatiques et à la démonstration : « envoyez-moi
votre export, on le regarde ensemble ».


## Import — correspondance des colonnes

> Étape 6, partie 2. Le récapitulatif avant import et le bouton
> « ouvrir dans ma messagerie » arrivent ensuite.

`http://127.0.0.1:8000/import` — accessible depuis le tableau de bord par
« Importer mes factures ».

Le garagiste envoie son export, voit **ses propres en-têtes**, et dit ce que
chacun représente. Une proposition est déjà remplie.

### Comment la proposition est faite

Deux signaux, combinés :

* **le nom de la colonne** — `N° facture`, `num_facture`, `invoice_no` et `Num`
  se ramènent tous au même après suppression des accents et de la ponctuation ;
* **le contenu de la colonne** — une colonne nommée `Total TTC` qui ne contient
  aucun montant lisible n'est pas proposée comme montant, quel que soit son nom.

Sur les trois exports d'exemple, aux en-têtes totalement différents, les quatre
champs obligatoires sont trouvés automatiquement dans les trois cas. Le fichier
B, qui sépare HT, TVA et TTC, voit bien le TTC retenu comme montant réclamé.

### Champs

Quatre suffisent à relancer une facture : **numéro**, **nom du client**,
**date de facture**, **total TTC**. Tout le reste est facultatif — e-mail,
échéance, acompte, HT, TVA, véhicule, immatriculation, prestation, téléphone,
adresse, code postal, ville.

Sans e-mail, un client reste importable : il n'est simplement plus relançable
par e-mail, seulement par courrier.

### Le délai de paiement est une question, pas une constante

Quand le fichier n'a pas de colonne d'échéance, l'écran de correspondance
demande **une fois** : « Vos factures sont payables sous quel délai ? » —
à réception, 15, 30, 45 ou 60 jours. La réponse est enregistrée dans le profil
d'import et n'est plus redemandée pour ce logiciel.

**30 jours est présélectionné.** Supposer trop long ne fait que retarder une
relance ; supposer trop court relance des clients qui ne sont pas en retard, et
c'est cette erreur-là qui coûte un client au garage.


### Profils

Une fois corrigée, la correspondance s'enregistre sous un nom de profil. Le
prochain export du même logiciel est reconnu à ses en-têtes et la
correspondance est déjà faite :

* en-têtes identiques → **reconnaissance exacte**, plus rien à renseigner ;
* logiciel qui a ajouté une colonne → **reconnaissance partielle**, signalée,
  à vérifier ;
* logiciel différent → aucun profil appliqué.

C'est ce qui couvre vingt logiciels sans écrire vingt connecteurs.

Les profils vivent dans `data/profils-import.json`, pas dans une base : une
remise à zéro de la démonstration ne les perd jamais.

### Ce qui n'est pas fait à cette étape

**Rien n'est écrit.** Le fichier reste en mémoire, le temps de la
correspondance. Un import abandonné ne laisse aucune trace.

Les vraies factures iront dans `data/garage-reel.db` — **jamais** dans
`data/garage.db`. `check_logic.py` vérifie que lire un fichier, deviner une
correspondance et enregistrer un profil laissent la base de démonstration
identique octet pour octet.


## Import — le deuxième import réconcilie

> Étape 6, partie 3.

Un garage importe toutes les semaines. Le deuxième fichier est en grande partie
le premier. Recréer ce qui existe déjà détruirait les deux choses que le
garagiste ne peut pas refaire à la main :

* **les conditions de pénalité qu'il a confirmées**, client par client, contre
  des CGV signées ;
* **l'historique de ses relances** — un niveau qu'il a marqué *Ignoré* serait
  reproposé, et les compteurs repartiraient de zéro chaque lundi.

Un ré-import n'est donc pas un import, c'est une réconciliation.

| | Règle |
| --- | --- |
| **Factures** | appariées sur le numéro. Même numéro = mise à jour, jamais insertion. Une mise à jour ne déplace que le **règlement** : montant réglé, statut, date de règlement. |
| **Total modifié** | ce n'est pas une mise à jour normale : la facture est **signalée et rien n'est appliqué**, ni le total, ni le règlement. Avoir ? refacturation ? c'est au garagiste de regarder. |
| **Clients** | appariés sur un nom normalisé — casse, accents, ponctuation, forme juridique et ordre des mots ignorés. « SARL Bonnet », « Bonnet SARL » et « BONNET S.A.R.L » sont un seul client. L'e-mail est une clé plus forte quand le fichier en porte un. |
| **Conditions de pénalité** | jamais touchées sur un client existant. Un nouveau client arrive **non confirmé**, comme toujours. |
| **Fiches clients** | seuls les champs vides sont complétés. Ce que le garagiste a saisi n'est jamais écrasé par un fichier. |
| **Absences** | une facture connue et absente du fichier n'est **jamais supprimée** — l'export couvre peut-être une période plus courte. Elle est comptée et nommée. |

### Le récapitulatif avant confirmation

Une page de confirmation dense ne se lit pas : elle se clique. Chaque
réassurance ajoutée y rend les autres moins visibles, jusqu'à ce que le
garagiste valide sans rien avoir lu — et alors la page entière ne vaut plus
rien. L'écran par défaut ne contient donc qu'une chose à lire :

```
3 factures déjà connues, inchangées.
3 factures vont être ajoutées.
2 lignes ont un problème et ne seront pas importées.
1 échéance a été calculée (absente du fichier).

[ Confirmer l'import ]   Voir le détail
```

Deux exceptions restent visibles sous ces lignes, parce qu'elles changent ce
qu'il doit **faire**, et pas seulement ce qu'il sait — une ligne chacune :

* une échéance calculée : le délai appliqué est peut-être le mauvais ;
* un client sans adresse e-mail : sa relance ne pourra partir que par courrier.

Tout le reste est derrière **Voir le détail**, replié mais intact : chaque ligne
écartée nommée et motivée, chaque facture signalée avec les deux totaux côte à
côte, les factures connues absentes du fichier, les nouveaux clients, les
adresses e-mail partagées, et les cinq premières factures telles qu'elles
seront comprises — montants et dates déjà reformatés. Cet aperçu ne montre que
des lignes **qui seront réellement écrites** : y faire figurer une ligne
destinée au rebut sous le titre « telles qu'elles seront comprises » promettrait
ce que l'import ne va pas faire.

« Ce que l'import ne fait jamais » n'est déplié qu'au **tout premier import**
dans un grand livre donné. Cette garantie se lit une fois ; répétée chaque
semaine au-dessus du bouton, elle apprend surtout à faire défiler la page.

Puis un bouton **Confirmer**. Avant lui, rien n'est écrit : le fichier reste en
mémoire, un import abandonné ne laisse aucune trace.

### La reconnaissance est un résultat

Un import qui ne change rien est le cas **normal** d'une semaine de garage : le
logiciel de facturation réexporte le même fichier, et rien n'a bougé. Un écran
qui ne rapporte que des différences rend ce cas illisible — « vu, rien à
changer » et « pas vu du tout » s'affichent exactement pareil, et le garagiste
n'a aucun moyen de savoir si la réconciliation a fonctionné.

Le récapitulatif dit donc toujours ce qui a été reconnu, en premier :

```
3 factures déjà connues, inchangées.
Aucune facture ne sera ajoutée.
2 lignes ont un problème et ne seront pas importées.
1 échéance a été calculée (absente du fichier).
```

Et l'écran d'après ne dit jamais « 0 mise(s) à jour » tout court, ce qui se lit
comme une panne :

> Import vérifié. 3 facture(s) déjà connue(s), aucune modification. 2 ligne(s)
> n'ont pas été importées. **Aucune écriture n’était nécessaire : les factures
> reconnues sont restées inchangées.**

**La règle, écrite sur la page et pas laissée à deviner :** une facture déjà
connue n'est comptée comme *mise à jour* que si son **règlement, sa date, son
échéance ou son libellé** a réellement changé. Les champs modifiés sont nommés
avant confirmation et les anciens brouillons non envoyés sont recalculés.
Réimporter un fichier identique ne produit donc aucune mise à jour, et c'est
correct. Un total, un client ou un véhicule incohérent est *signalé* et n’est
jamais modifié automatiquement.

### Le nom fait le client, pas l'adresse e-mail

L'identité d'un client, c'est son nom. Une adresse e-mail n'est qu'un moyen de
le joindre, et **deux clients réellement différents partagent souvent la même** :
une auto-école avec plusieurs véhicules, une flotte suivie par un seul
gestionnaire, ou le garagiste qui saisit sa propre adresse faute d'avoir celle
du client.

L'asymétrie tranche, comme pour le séparateur ambigu :

* fusionner à tort envoie une relance **au mauvais nom pour la dette d'un
  autre** — et c'est le client qui la lit ;
* ne pas fusionner à tort crée une fiche client en double — que personne ne
  voit.

Donc : **noms normalisés différents = clients différents**, dans le fichier
comme face à la base. La normalisation ayant déjà absorbé la casse, les accents,
la ponctuation, la forme juridique et l'ordre des mots, un nom qui diffère
encore après elle est une vraie différence, pas une faute de frappe.

L'adresse commune est quand même signalée, sur la facture concernée — une des
deux est peut-être erronée, et seul le garagiste peut le dire :

> **F-302** — « SARL Béton Léger » et « Nathalie Dupré » partagent l'adresse
> partage@exemple.fr.
> Ils sont traités comme deux clients distincts. Si c'est le même, corrigez
> l'adresse et réimportez.

Le rattachement se fait sur le nom seul aux deux étages — dans le plan affiché
et à l'écriture — sans quoi l'écran annoncerait une chose et la base en ferait
une autre. Un ré-import du même fichier reste idempotent : les noms
correspondent, aucun doublon n'est créé.

### Deux bases séparées

Les factures réelles vont dans `data/garage-reel.db`. Le jeu de démonstration
reste dans `data/garage.db` et n'est jamais touché — `check_logic.py` le vérifie
octet pour octet après un cycle d'import complet, et `reset-demo.bat` restaure
toujours exactement le même état.

### Les fichiers d'exemple du ré-import

`samples/export-logiciel-A-semaine2.csv` est le même export une semaine plus
tard : une facture réglée entre-temps, une nouvelle, deux absentes, et un client
écrit « BOULANGERIE CHEZ LEA S.A.R.L. » au lieu de « Boulangerie Chez Léa ».
C'est le fichier qui prouve que le second import réconcilie au lieu d'insérer.


## Ouvrir la relance dans sa messagerie

> Étape 6, partie 4.

Une relance validée par e-mail se télécharge en `.eml`. Le garagiste l'ouvre,
son Outlook affiche le message **déjà adressé et déjà rédigé**, et c'est lui
qui clique sur Envoyer.

Le message part donc de sa propre adresse et atterrit dans ses propres
éléments envoyés. **Le système ne détient aucun mot de passe de messagerie,
aucun jeton OAuth, aucun accès SMTP — et n'en détiendra jamais.**

### Ce que contient le fichier

| | |
| --- | --- |
| `To` | l'adresse du client |
| `Subject` | encodé MIME quand il porte des accents, sinon il arrive en charabia |
| Corps | UTF-8, avec le jeu de caractères déclaré |
| `X-Unsent: 1` | ce qui fait qu'Outlook l'ouvre **en rédaction** et non comme un message reçu |
| `From` | **absent volontairement** — la messagerie met l'adresse du garagiste |

Sur cette machine, `.eml` est associé à
`OUTLOOK.EXE /eml "%1"` : le fichier s'ouvre bien dans le client de messagerie
au lieu d'être bloqué.

### Deux routes, toujours proposées ensemble

Un `.eml` ne sert à rien pour qui lit son courrier sur gmail.com : il télécharge
un fichier que rien n'ouvre. Les deux routes sont donc offertes côte à côte,
sans détection — le garagiste sait laquelle est la sienne.

* **Logiciel de messagerie** — le fichier `.eml` décrit ci-dessus ;
* **Gmail** — un lien vers la fenêtre de rédaction de Gmail, destinataire, objet
  et corps déjà remplis. Rien n'est connecté : pas d'OAuth, pas d'identifiant,
  aucun accès à la boîte.

### La limite de longueur, mesurée

Une adresse web peut être tronquée en silence, et une relance coupée en deux
qui part est exactement le dommage à éviter.

Mesuré sur les vrais messages du système : de **1 454 caractères** (niveau 1) à
**2 569** (niveau 4), l'encodage doublant presque le corps à cause des accents
et des retours à la ligne. Le navigateur n'est pas le facteur limitant — une
sonde locale a fait passer 32 000 caractères intacts. La limite propre à Gmail
n'a pas pu être mesurée sans compte.

Le seuil est donc fixé à **2 000 caractères**, la longueur d'URL universellement
sûre, plutôt qu'à une estimation optimiste. Au-delà, **aucun lien pré-rempli
n'est proposé** : deux boutons *Copier l'objet* / *Copier le message* et une
fenêtre Gmail vide adressée au bon client.

En pratique : niveau 1 toujours en lien direct, niveaux 2 et 3 partagés, niveau 4
jamais concerné puisqu'il part par courrier.


### Remis n'est pas envoyé

Le système ne peut pas savoir si le garagiste a cliqué sur Envoyer ou fermé la
fenêtre. Alors il ne le prétend pas :

1. le `.eml` est téléchargé → la relance passe à **« Remise à votre
   messagerie »**. Elle n'est pas comptée comme envoyée ;
2. la file affiche **« Ce message a bien été envoyé ? »** ;
3. seul ce clic la fait passer à **envoyée** et l'ajoute aux compteurs.

Une relance remise mais non confirmée reste visible : elle ne se perd pas.
C'est le même refus que pour « montant récupéré » — ne compter que ce qu'on
sait vraiment.

### Les brouillons oubliés

Gmail conserve toute fenêtre de rédaction ouverte comme brouillon. Un garagiste
qui ouvre 23 relances et en envoie 15 se retrouve avec 8 brouillons en attente.
Le système ne touche pas à sa messagerie — par construction — mais un brouillon
oublié ne doit pas rester dangereux : envoyé trois semaines plus tard, il
relance un client qui a peut-être déjà payé. C'est la même classe de dommage
qu'un montant faux.

Trois protections :

1. **Une liste dédiée sur le tableau de bord** — « Ouvertes mais non
   confirmées », avec la phrase qui fait le lien : *si un brouillon traîne dans
   votre messagerie, c'est l'un de ceux-là*.
2. **Fermeture automatique au ré-import** — quand la réconciliation constate
   qu'une facture est réglée ou que son solde/échéance change, toute relance
   non envoyée — y compris déjà validée — passe à *sans objet*, sort de la file,
   et est nommée sur le récapitulatif :
   *supprimez le brouillon s'il en reste un*. Une relance fermée n'est jamais
   comptée comme envoyée.
3. **Avertissement au doublon** — rouvrir un message déjà ouvert affiche la date
   de la première ouverture avant d'en créer un second. Le garagiste peut
   continuer, en connaissance de cause.


### Le niveau 4 reste du papier

Une mise en demeure ne devient jamais un e-mail. Le bouton **Imprimer** reste
la seule voie, et demander le `.eml` d'une relance de niveau 4 est refusé
explicitement.


## Connecter la messagerie Google (recommandé)

`http://127.0.0.1:8000/parametres/envoi`

Un clic, le garagiste choisit son compte Google, c'est terminé. Rien à activer,
aucun code à recopier. C'est la voie qui ne fait pas abandonner en rendez-vous.

### Les autorisations demandées

| Autorisation | Classement Google | À quoi elle sert |
| --- | --- | --- |
| `https://www.googleapis.com/auth/gmail.send` | sensible | envoyer, et rien d'autre |
| `openid` | de base | identifier le compte connecté |
| `email` | de base | connaître son adresse |

**Une seule autorisation Gmail**, et c'est celle d'envoi.

Ce scope est classé *sensible* : il ne demande qu'une vérification légère. Tous
ceux qui permettent de **lire** une boîte — `gmail.readonly`, `gmail.modify`,
`gmail.metadata`, `mail.google.com` — sont classés *restreints* et déclenchent
un audit de sécurité payant par un tiers, qui prend des mois.

Le système n'a jamais besoin de lire la boîte : la détection des règlements
vient du ré-import du fichier du garage, pas de sa messagerie.

`openid` et `email` sont classées **non sensibles** : elles ne changent ni le
niveau de vérification, ni le risque d'audit. Elles sont là pour une raison
précise — savoir **quel compte** vient d'être connecté (voir l'avertissement
ci-dessous).

Un contrôle de `check_logic.py` parcourt **tout le code source** et échoue si la
moindre chaîne d'un scope de lecture y apparaît. Un autre vérifie que `SCOPES`
contient exactement ces trois autorisations, et rien de plus. C'est ce contrôle qui protège
du basculement accidentel vers l'audit long. Il est volontairement brutal — une
simple recherche de sous-chaîne — au point que le lien « Ouvrir dans Gmail » a dû
être construit à partir d'une constante d'hôte, parce que l'URL de rédaction
contient mot pour mot la chaîne d'un scope restreint.

### « Ce n'est pas l'adresse du garage »

Beaucoup de garagistes ont deux comptes Google : un personnel et un
professionnel. Après avoir cliqué, rien à l'écran ne dit lequel a été choisi —
et des relances clients partant de son Gmail personnel, il s'en aperçoit des
semaines plus tard.

L'adresse connectée est donc affichée, sur l'écran des paramètres **et** sur le
tableau de bord. Quand elle ne correspond pas à l'adresse du garage
(`GARAGE["email"]` dans `config.py`), un avertissement apparaît juste au-dessus
de la file de validation :

> **Les relances partiront de julien.perso@example.com** — ce n'est pas
> l'adresse du garage (contact@garage-martel.example.com). Si vous avez connecté
> votre compte personnel par erreur, changez de compte avant d'envoyer. Rien
> n'est bloqué : c'est votre choix.

**C'est un avertissement, jamais un blocage.** C'est son compte et sa décision ;
un contrôle vérifie qu'un envoi aboutit malgré le décalage. La comparaison
ignore la casse et les espaces.

L'adresse est lue dans le jeton d'identité renvoyé par Google, jamais devinée.


### Ce que voit le garagiste

1. Il clique sur **Connecter ma messagerie Google**.
2. Google lui demande de choisir son compte.
3. Un écran d'avertissement s'affiche : **« Google n'a pas validé cette
   application »**. C'est normal tant qu'elle n'est pas publiée. Il faut cliquer
   sur **Paramètres avancés**, puis **Accéder à Relance Garage (non sécurisé)**.
   L'écran des paramètres l'annonce à l'avance, mot pour mot, pour qu'il ne
   s'arrête pas là.
4. Google demande d'autoriser l'envoi de messages en son nom, et de partager
   son adresse.
5. Il revient automatiquement sur l'écran des paramètres, connecté.

### Application en mode test : deux conséquences

L'application est en **mode test** dans la console Google, pas publiée — publier
exige une page d'accueil, une politique de confidentialité et des conditions
d'utilisation, donc un nom de domaine.

**La connexion expire au bout de 7 jours.** C'est une propriété du mode test,
pas un défaut. Quand cela arrive :

* la relance **reste validée et non envoyée** — elle n'est jamais perdue, ni
  marquée envoyée à tort ;
* l'écran affiche **« La connexion à votre messagerie a expiré —
  reconnectez-vous »**, avec le bouton de reconnexion juste à côté ;
* se reconnecter est un clic.

L'écran des paramètres affiche la date d'expiration et les jours restants, et
prévient à partir de deux jours.

**Seuls les comptes déclarés comme utilisateurs de test peuvent se connecter.**
Si Google refuse pour cette raison, le message l'explique en français et indique
d'ajouter l'adresse dans la console Google Cloud — jamais une erreur brute.

### Quand vous aurez un domaine

Il faudra, dans la console Google Cloud, renseigner une page d'accueil, une
politique de confidentialité et des conditions d'utilisation, puis demander la
publication en **Production** avec vérification du scope sensible `gmail.send`.
Une fois publiée :

* la connexion **n'expire plus au bout de 7 jours** ;
* **n'importe quel compte Google** peut se connecter, plus seulement les
  utilisateurs de test ;
* l'écran **« application non validée » disparaît**.

Le code n'a pas à changer : c'est un changement d'état côté console.

### Où vit l'autorisation

Dans `data/google-token.json`, sur la machine du garage, ignoré par git, jamais
dans une base de données ni dans le jeu de démonstration. L'identifiant client
(`google-client.json`) est ignoré par git lui aussi.

Le garagiste peut retirer l'accès lui-même à tout moment depuis
[myaccount.google.com/permissions](https://myaccount.google.com/permissions) —
l'envoi s'arrête aussitôt, et les relances restent téléchargeables. Le bouton
**Déconnecter** fait la même chose et révoque le jeton côté Google.

### Trois voies, au choix du garagiste

| Voie | Ce qu'elle demande | Pour qui |
| --- | --- | --- |
| **Google** | un clic, un compte Google | la plupart des garages |
| **SMTP** | un mot de passe d'application | Outlook, OVH, Gandi, autres |
| **`.eml` / lien Gmail** | rien du tout | ceux qui ne veulent rien connecter |

Les deux dernières ne disparaissent jamais. Quand un compte Google est connecté,
c'est lui qui est utilisé pour le bouton **Envoyer**.


## Envoi automatique par SMTP (facultatif)

`http://127.0.0.1:8000/parametres/envoi`

Pour une messagerie qui n'est pas Google : Outlook, OVH, Gandi, un hébergeur
quelconque. Demande un mot de passe d'application, à créer dans le compte
concerné.

### Le mot de passe d'application

Ce n'est **pas** le mot de passe du compte. C'est un code de 16 caractères que
le garagiste génère lui-même dans son compte Google :

1. activer la **validation en deux étapes** (obligatoire) ;
2. Sécurité → **Mots de passe des applications** ;
3. en créer un nommé « Relance Garage » et recopier le code.

Il ne permet que d'envoyer du courrier, ne donne accès ni aux messages ni au
reste du compte, et se **révoque en un clic**. Aucune validation Google n'est
nécessaire, contrairement à l'API Gmail.

### Préréglages

| Fournisseur | Serveur | Port |
| --- | --- | --- |
| Gmail / Google Workspace | `smtp.gmail.com` | 587 (STARTTLS) |
| Microsoft 365 / Outlook | `smtp.office365.com` | 587 (STARTTLS) |
| OVH | `ssl0.ovh.net` | 587 (STARTTLS) |
| Gandi | `mail.gandi.net` | 587 (STARTTLS) |

Le bouton **Tester la connexion** envoie un seul message à la propre adresse du
garagiste et rapporte le résultat en français.

### Où vivent les identifiants

Dans `data/envoi-smtp.json`, sur la machine du garage, **en clair**, ignoré par
git. Jamais dans une base de données, jamais dans le jeu de démonstration.

Il faut le dire tel quel au garagiste : **qui a accès à cet ordinateur a accès
à ce code**. En contrepartie, ce code ne sert qu'à envoyer du courrier et se
révoque en un clic depuis son compte — l'envoi s'arrête aussitôt et les
relances restent téléchargeables.

### Les règles qui ne bougent pas

* **Rien ne part sans que le garagiste ait validé ce message précis.** La barrière
  est dans le code : une relance non validée ne peut pas être envoyée, quoi que
  demande l'appelant.
* **`DEMO_MODE=1` : aucune relance ne part.** Le contrôle est la première
  instruction de `send()` — aucune socket ouverte, aucune résolution DNS. Un
  contrôle de `check_logic.py` remplace `smtplib.SMTP` par un piège qui explose
  si on l'instancie, et vérifie qu'il n'est jamais déclenché.
* **Le niveau 4 ne devient jamais un e-mail.**
* Un envoi réussi passe directement à *envoyée* — pas de question « l'avez-vous
  envoyé ? », puisqu'ici le système le sait vraiment.
* **Un envoi qui échoue laisse la relance validée et non envoyée**, jamais
  marquée envoyée en silence.

### En démonstration, le bouton montre l'envoi

Le bouton **Envoyer** est visible et cliquable en mode démonstration, même sans
aucune configuration SMTP sur la machine — l'écran du prospect ne doit pas
dépendre du fichier de réglages du vendeur.

Le clic ouvre un **aperçu** : expéditeur, destinataire, objet et corps du
message exactement tels qu'ils partiraient, avec la mention
**« Démo · aucun envoi réel »**. La relance passe tout de même à *envoyée*, donc
la file et les compteurs se comportent devant le prospect comme ils se
comporteront chez lui.

Aucune socket n'est ouverte sur ce chemin : `sending.send()` n'est même pas
appelé, et un contrôle le vérifie de bout en bout via l'API.

### L'exception : « Tester la connexion »

Ce bouton **fonctionne en mode démonstration**, et c'est délibéré. Il écrit à
**l'adresse du garagiste lui-même**, jamais à un client : c'est une étape de
configuration, pas une relance. L'obliger à quitter le mode démonstration pour
vérifier un mot de passe d'application serait absurde.

L'exemption est explicite dans le code (`send(..., allow_in_demo=True)`) et un
seul appelant l'utilise. Deux contrôles l'encadrent : une relance en mode démo
n'ouvre **aucune** connexion, le test de connexion en ouvre **une**.


### Quand ça échoue

Chaque panne a son message et sa marche à suivre : mot de passe d'application
refusé, validation en deux étapes non activée, quota journalier atteint
(≈ 500 destinataires par jour sur un Gmail gratuit), serveur injoignable,
adresse d'expéditeur refusée, destinataire refusé. Dans tous les cas, les voies
`.eml` et Gmail restent disponibles.


## Lecture des données

```bash
python scripts/show_data.py                        # synthèse + créances
python scripts/show_data.py --lines FA-2026-1213   # détail d'une facture
python scripts/show_overdue.py                     # encours + file de relance
python scripts/show_overdue.py --all               # état de chaque facture
python scripts/check_logic.py                      # contrôles de calcul

python scripts/draft_followups.py                  # rédige les relances du jour
python scripts/draft_followups.py --show 4         # affiche les mises en demeure
python scripts/draft_followups.py --one FA-2026-1202
python scripts/draft_followups.py --demo-guardrail # le contrôle des chiffres en action
python scripts/draft_followups.py --prompt FA-2026-1202
python scripts/draft_followups.py --reset          # repartir d'une file vide

python scripts/test_api_mode.py                    # un seul appel API réel, rien n'est enregistré
python scripts/reset_demo.py                       # remise à zéro avant une démo
```

## Configuration

Voir `.env.example`. Les variables utiles :

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `DEMO_MODE` | `1` | Aucun envoi réel, aperçu seulement |
| `LLM_MODE` | `mock` | `mock` (hors ligne, gratuit) ou `anthropic` (API réelle) |
| `ANTHROPIC_API_KEY` | — | Requis uniquement si `LLM_MODE=anthropic`. À placer dans un fichier `.env` à la racine, jamais dans le code |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Modèle utilisé en mode API (modifiable sans toucher au code) |
| `ANTHROPIC_EFFORT` | `medium` | Effort de rédaction (`low` … `max`) |
| `ANTHROPIC_MAX_TOKENS` | `1200` | Limite de sortie d’une relance |
| `DEMO_TODAY` | — | Fige la date de référence, pour des captures reproductibles |
| `DEMO_SEED` | `20260827` | Graine du générateur |
| `GARAGE_DB` | `data/garage.db` | Jeu fictif réinitialisable |
| `GARAGE_REAL_DB` | `data/garage-reel.db` | Base des factures réelles importées |
| `GARAGE_SMTP` | `data/envoi-smtp.json` | Identifiants SMTP (ignoré par git) |
| `GOOGLE_CLIENT` | `google-client.json` | Identifiant OAuth de bureau (ignoré par git) |
| `GOOGLE_TOKEN` | `data/google-token.json` | Autorisation Google (ignoré par git) |

## Structure

```
start.bat          lancement en un clic
reset-demo.bat     remise à zéro avant une démonstration
app/
  config.py        identité du garage, TVA, seuils d'escalade, pénalités
  money.py         arithmétique en centimes entiers + format français
  db.py            schéma SQLite
  catalog.py       catalogue des prestations (pièces + main d'œuvre)
  labels_fr.py     tout le vocabulaire français, au même endroit
  demo_roster.py   les 24 clients fictifs et leurs véhicules
  demo_plan.py     l'encours placé à la main + l'historique généré
  seed.py          générateur + contrôles de cohérence
  overdue.py       détection des retards, escalade, pénalités, agrégats
  drafting.py      le contexte transmis au moteur — chaînes finies uniquement
  mock_drafts.py   messages pré-rédigés, un ton par niveau
  anthropic_backend.py  moteur API (désactivé par défaut)
  guardrail.py     contrôle des chiffres inventés
  followups.py     enregistrement des brouillons et file de validation
  kpi.py           compteurs sur 30 jours glissants, clients sans CGV
  server.py        l'application FastAPI et les actions de la file
  templates/dashboard.html   la page unique du tableau de bord
  templates/letter.html      la mise en demeure, prête à imprimer
  importer/values.py         montants et dates -> centimes entiers et dates
  importer/reader.py         encodage, séparateur, CSV et Excel
  importer/convention.py     ce que « , » et « . » veulent dire dans CE fichier
  importer/mapping.py        correspondance des colonnes et profils
  importer/reconcile.py      appariement factures et clients au ré-import
  importer/runner.py         lecture des lignes mappées et écriture confirmée
  eml.py                     le fichier .eml et le lien Gmail
  sending.py                 envoi SMTP optionnel, avec mot de passe d'application
  google_mail.py             connexion Google OAuth, envoi seul (gmail.send)
  templates/settings_smtp.html   écran des paramètres d'envoi
  web_import.py              les écrans d'import
  templates/import_upload.html   envoi du fichier
  templates/import_mapping.html  correspondance des colonnes
  templates/import_summary.html  récapitulatif et confirmation
scripts/
  show_data.py     lecture des factures en terminal
  show_overdue.py  encours et file de relance
  check_logic.py   preuve que tous les calculs sont en Python
  draft_followups.py  rédaction des relances et aperçu des messages
  test_api_mode.py    un appel réel à l'API, sur une seule facture
  reset_demo.py       remise à zéro avant une démonstration
  make_samples.py     crée les fichiers d'exemple dans samples/
  inspect_file.py     lit un export et montre ce qui est reconnu
  open_browser.py  ouvre le tableau de bord quand le serveur répond
data/
  garage.db        base générée (non versionnée)
```

## Sécurité du jeu de démonstration

Les clients sont fictifs. Toutes les adresses e-mail utilisent le domaine
réservé `example.com` et les numéros de téléphone sont pris dans des plages
réservées à la fiction : même en cas de bascule accidentelle hors du mode démo,
aucune personne réelle ne peut être contactée.

## Avancement

- [x] **Étape 1** — modèle de données et générateur de factures
- [x] **Étape 2** — détection des retards et logique d'escalade
- [x] **Étape 3** — rédaction des messages + contrôle des chiffres
- [x] **Étape 4** — tableau de bord
- [x] **Étape 5** — file de validation + parcours de démonstration
- [x] **Étape 6** — import des factures réelles
  - [x] lecture des fichiers (encodages, séparateurs, montants, dates)
  - [x] écran de correspondance des colonnes + profils enregistrés
  - [x] réconciliation du deuxième import (factures et clients)
  - [x] récapitulatif avant import et confirmation
  - [x] bouton « ouvrir dans ma messagerie » (.eml)
