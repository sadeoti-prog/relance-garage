# Revue complémentaire — simplicité et brouillon Gmail

Date : 5 septembre 2026. Projet modifié directement dans le dossier original.
Ce compte rendu complète l’audit précédent ; il ne constitue pas une validation
de mise en production.

## 1. Problèmes importants trouvés

- Un aperçu en démonstration marquait une relance « envoyée » sans envoi.
- Le tableau de bord pouvait annoncer « Tout est à jour » malgré des messages
  validés ou des envois manuels à confirmer.
- La file affichait « aucune relance à vérifier » de manière rassurante alors
  que des messages restaient à préparer.
- Les cartes clients répétaient coordonnées et conditions, sans recherche.
- La connexion Google existante autorise l’envoi, pas la création de brouillons
  par l’API. Les deux capacités ne devaient pas être confondues.
- Le lanceur pouvait choisir un moteur payant à partir de la configuration.

## 2. Frontend

Tableau de bord recentré sur le montant à encaisser et la prochaine action.
Le suivi détaillé reste accessible dans une section repliable. Suppression
d’un avertissement de démonstration répété ; le mode reste visible en permanence.

Clients présentés dans une liste avec recherche insensible aux accents,
majuscules et espaces périphériques. Coordonnées et conditions restent
accessibles à la demande. État sans résultat explicite.

Relances validées clairement indiquées comme non envoyées. Aperçu avec statut
fidèle. Accès au test Gmail depuis Paramètres et l’aperçu d’une relance.
Suppression du double libellé « Réglée » dans les factures.

## 3. Backend

Les aperçus ne changent plus le statut ni la date d’envoi, dans aucun contexte.
Le test Gmail produit un contenu fixe fictif, adressé uniquement au compte
connecté. Il ne lit aucune facture, ne contacte aucune API et ne permet pas
de choisir un destinataire client. Le lien cible le compte Google affiché.
Les adresses invalides sont refusées et la limite de longueur du lien inclut
désormais le compte choisi.

Le lanceur demande explicitement les modèles locaux gratuits. Aucun moteur IA
payant n’a été activé.

## 4. Base de données

Aucune migration. Aucun import ni modification des factures réelles pour ces
essais. Les données fictives ont été utilisées pour préparer puis valider une
relance de test. Empreinte SHA-256 des données réelles identique avant et après
les vérifications :

`3CB69B01288808138F307236B9C9B39CC1F762FEF64160B943943B00F31E1933`

## 5. Règles importantes

- Démonstration par défaut ; données réelles séparées.
- Aperçu ou brouillon ne signifie jamais « envoyé ».
- Test Gmail : exemple fictif et destinataire égal au compte connecté.
- Le site ouvre Gmail prérempli ; il ne crée pas le brouillon par l’API Google.
- L’enregistrement doit être vérifié dans Gmail. Le site n’a pas accès à la boîte.
- La remise à zéro reste exclusivement dédiée à la démonstration.
- Les factures identiques restent reconnues mais inchangées, sans faux ajout.

## 6. Cinq passes de vérification

| Passe | Vérifications et résultat |
|---|---|
| Visuelle / UX | Tableau de bord, factures, clients, import, relances, paramètres et test Gmail inspectés. Contrôles complémentaires à 390 px sur tableau de bord, clients, factures, import et aperçu. Pas de débordement horizontal constaté sur les écrans vérifiés. Informations secondaires repliées, messages contradictoires corrigés. |
| Frontend fonctionnelle | Navigation, filtres, recherche « AURELIE » retrouvant « Aurélie », recherche sans résultat, ouverture des coordonnées, préparation et validation, ouverture/fermeture répétée de l’aperçu, passage au test Gmail. Aucune erreur JavaScript relevée sur le parcours contrôlé. |
| Backend / données | Suites métier et intégration relancées : rapprochement, imports inchangés/modifiés/invalides, seuils de relance, concurrence, persistance, transitions. Tests supplémentaires : aperçus répétés sans statut envoyé, adresse invalide, confidentialité et absence d’accès aux bases pour le test Gmail. |
| Séparation et sécurité | Basculements réels Démo → Réel → Démo → Réel → Démo : 70 factures fictives contre 3 réelles, affichage du bon mode. Empreinte des données réelles inchangée. Réinitialisation atomique et isolation de la base réelle retestées sur bases temporaires par la suite de sécurité ; pas de nouvelle remise à zéro destructive des données originales. |
| Parcours dirigeant | Tableau de bord → préparation → vérification de FA-2026-1244 → validation → aperçu non envoyé → test Gmail → ouverture du message fictif dans le bon Gmail. Un seul nouveau brouillon de test observé dans Brouillons après fermeture de sa fenêtre. Aucun clic d’envoi. |

## 7. Tests automatisés

- `scripts/check_logic.py` : 621 contrôles réussis.
- `scripts/check_product_safety.py` : 90 contrôles réussis.
- `scripts/check_gmail_demo.py` : 7 tests réussis.
- Compilation Python de `app` et `scripts` : réussie.
- `scripts/draft_followups.py --mock` : exécuté, aucun crédit consommé.
- Pas de configuration de lint, de typage ou de build frontend distinct dans
  ce projet HTML/Jinja/JavaScript.

## 8. Services et coûts

Gmail est utilisé dans le navigateur pour ce test, sans extension des permissions
OAuth. L’intégration d’envoi Google côté serveur n’a volontairement pas été
testée par un envoi réel. SMTP reste une autre option d’envoi.

Claude est facultatif : il reformule les relances à partir de chiffres déjà
calculés par l’application. Il ne calcule pas les montants et ne détermine pas
les niveaux. La version courante fonctionne avec des textes locaux sans API.
Activer le backend Anthropic nécessiterait une facturation API distincte.

ChatGPT Plus n’inclut pas l’usage facturé de l’API OpenAI : il ne peut pas être
transféré au site sous forme de crédits API. Pour ce MVP, conserver les modèles
de messages gratuits et améliorer leur rédaction ici est suffisant.

Sources : [facturation OpenAI](https://help.openai.com/en/articles/9039756),
[facturation Anthropic](https://support.claude.com/en/articles/9876003-i-have-a-paid-claude-subscription-pro-max-team-or-enterprise-plans-why-do-i-have-to-pay-separately-to-use-the-claude-api-and-console),
[autorisations nécessaires aux brouillons Gmail](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/create).

## 9. Points avant production

Indispensable : conserver l’accès local tant que l’authentification et les
protections d’hébergement ne sont pas mises en place ; sécuriser les secrets ;
confirmer les conditions commerciales ; tester explicitement les envois réels
et les cas de reprise après incident. Le test de brouillon ne valide pas un
envoi réel par l’API Google.

Gmail signale un espace de stockage saturé : à résoudre avant de compter sur
ce compte pour des envois réels. Aucun réglage de stockage ou de facturation
du compte n’a été modifié.

Plus tard : recherche serveur/pagination pour un gros volume de clients,
supervision des échecs et sauvegardes/restaurations régulièrement éprouvées.
L’IA payante n’est pas une condition de mise en service.

## 10. Démonstration conseillée

1. Rester en Démonstration et montrer ce qui reste à encaisser.
2. Ouvrir une facture en retard, vérifier puis valider sa relance.
3. Montrer l’aperçu : « rien n’est envoyé sans votre intervention ».
4. Ouvrir le test Gmail fictif, montrer uniquement son brouillon, pas la boîte
   personnelle entière. Ne pas envoyer.

READY FOR DEMO WITH MINOR ISSUES

Le parcours de démonstration est cohérent et le brouillon a été vérifié. La
production reste désactivée et l’avertissement de stockage Gmail doit être
traité avant de tester de vrais envois.
