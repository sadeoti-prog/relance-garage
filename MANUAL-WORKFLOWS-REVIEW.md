# Clients et paiements manuels — revue du 5 septembre 2026

## 1. Problèmes importants

L’utilisateur dépendait d’un import pour créer un client ou une facture, et ne
pouvait pas renseigner un règlement reçu autrement. Le test Gmail générique ne
montrait pas la relance de son propre scénario. Lors des essais, deux ambiguïtés
supplémentaires ont été corrigées : le niveau de relance affiché sur une facture
réglée et la préparation de toutes les factures depuis le bouton d’une seule fiche.

## 2. Interface

Ajout et modification des clients, coordonnées secondaires repliables, fiche
client avec ses factures. Ajout d’une facture existante. Bouton « Marquer comme
payée » et formulaire simple pour paiement partiel/correction. Historique des
paiements repliable, confirmation des succès et des opérations sans changement.
Les erreurs conservent la saisie et indiquent la correction attendue.

## 3. Backend

Validation des montants en centimes, dates, e-mails et champs bornés. Transactions
atomiques, protection contre les formulaires périmés et les actions d’un autre
contexte. Création concurrente d’une même facture refusée. Coordonnées ou paiements
modifiés rendent les anciennes relances non envoyées obsolètes. Une préparation
depuis la fiche facture ne traite que celle-ci. Aucun nouveau service externe.

## 4. Données

Nouvelle table `payment_changes`, créée automatiquement dans chaque base, avec
montant précédent, nouveau montant, date, note et horodatage. Aucune colonne
existante supprimée, aucune facture existante effacée. Les paiements manuels sont
protégés contre un export contradictoire. Le test compare les empreintes de toutes
les lignes des tables clients/factures/véhicules/lignes/relances réelles avant et
après bascules : toutes inchangées. La nouvelle table réelle est vide.

La saisie est un suivi TTC, pas un logiciel d’émission de factures : aucune TVA
n’est inférée ni exposée comme un calcul fiscal. Les champs fiscaux obligatoires
du schéma historique restent des valeurs techniques non utilisées par les relances
pour les factures `manual_tracking` ; ne pas les utiliser pour un export comptable.

## 5. Règles

- Le paiement appartient à la facture, pas au client entier.
- Partiel = cumul reçu, jamais addition automatique d’un nouveau versement.
- Impayée remet le montant reçu à zéro ; l’écran le dit avant l’enregistrement.
- Un changement de montant/date/note est traçable. Une saisie identique ne crée
  pas de faux changement.
- Une facture payée ne reçoit plus de relance. Les envois historiques sont conservés.
- Un fichier contraire à un paiement manuel est écarté avec une explication ;
  le propriétaire vérifie/corrige la facture avant un nouvel import.
- Démo par défaut après redémarrage ; aucune modification du contexte réel depuis
  un formulaire rendu en démo. La remise à zéro ne vise que la démo, y compris ses
  ajouts manuels : avertissement ajouté dans Paramètres.
- Gmail reçoit une copie explicitement marquée TEST DÉMO pour le compte connecté,
  jamais le destinataire réel d’une autre facture. Une copie n’est pas un envoi.

## 6. Cinq passes de vérification

1. **Visuel / UX** : revue des formulaires clients, facture, paiement, fiche facture
   et aperçu Gmail sur ordinateur. Informations secondaires repliées, boutons et
   champs cohérents. Correction de « Acompte versé » en « Partiellement payée » et
   du niveau trompeur sur facture réglée. Limite : la commande de viewport mobile
   du navigateur de test n’a pas été appliquée (largeur restée à 1280 px malgré
   une demande de 390 px). Le CSS responsive a été examiné, mais une inspection
   visuelle mobile de ces nouvelles pages reste à faire ; elle n’est pas déclarée réussie.
2. **Fonctionnel navigateur** : client « Client test visio » créé avec l’e-mail
   fourni ; coordonnées complémentaires ; facture DEMO-VISIO-001 ; erreur de
   montant nul compréhensible avec conservation des champs ; correction et ajout
   réussi. Paiement total, partiel, retour impayé et nouvelle préparation testés
   par les vrais boutons, sans écriture directe pour simuler le parcours.
3. **Backend / persistance** : 27 nouveaux tests isolés couvrent noms normalisés,
   e-mails partagés, validation, concurrence, retour arrière transactionnel,
   formulaires périmés, dates, statuts, import contradictoire, préparation ciblée,
   historique et copie Gmail. Après plusieurs redémarrages du serveur, le client,
   la facture et les corrections persistent. À J+34 le niveau 3 est vérifié.
4. **Isolation démo/réel** : bascules réelles Démo → Réel → Démo → Réel → Démo.
   DEMO-VISIO-001 visible seulement en démo, F-001 seulement en réel, contexte
   explicite. Empreintes des données réelles identiques avant/après. Réinitialisation
   et retour arrière testés par la suite de sécurité sur des bases temporaires ;
   aucune réinitialisation destructrice de la démo personnalisée n’a été exécutée.
5. **Parcours dirigeant complet** : client → facture 120 € échue au 26/08/2026 →
   10 jours de retard / relance ferme au 05/09 → marquée payée (0 €) → partiellement
   payée à 20 € (100 € restant) → impayée (120 €) → nouveau message → validation →
   aperçu → Gmail. Un nouveau brouillon de DEMO-VISIO-001 a été réellement ouvert
   via le bouton du site puis retrouvé dans le dossier Brouillons du bon compte.
   Aucun clic sur Envoyer. La relance est toujours validée et non envoyée.

## 7. Contrôles automatisés

- `scripts/check_logic.py` : 621 contrôles réussis. L’ancien test du générateur
  supposait 70 factures dans la démo de l’utilisateur : corrigé pour utiliser un
  jeu de données en mémoire, car une démo personnalisable peut avoir davantage
  de factures. Aucun ajout utilisateur n’a été supprimé pour faire passer le test.
- `scripts/check_product_safety.py` : 90 contrôles réussis.
- `scripts/check_gmail_demo.py` : 7 tests réussis.
- `scripts/check_manual_workflows.py` : 27 tests réussis.
- Total : 745 contrôles/tests. `compileall app scripts` réussi.
- Pas de commande lint, typecheck ou build frontend configurée dans ce projet.

## 8. Services externes

Aucun service ni dépendance ajouté. La rédaction reste locale/gratuite en démo.
Le parcours Gmail ouvre le navigateur : il ne crée pas de brouillon par l’API et
n’élargit pas les autorisations OAuth. Il nécessite une session Gmail ouverte.
Les messages trop longs disposent d’un mode copier/coller, sans troncature.

## 9. Limites et production

Avant production : authentification avant toute exposition réseau, sécurisation
des identifiants/configuration OAuth et vérification séparée du transport réel
des e-mails. Ces changements ne prétendent pas valider l’envoi réel.

À vérifier ensuite : rendu mobile dans un navigateur appliquant le viewport,
export comptable si un tel besoin apparaît (hors périmètre de ce suivi TTC).
Un brouillon déjà transféré dans Gmail ne peut pas être retiré par une correction
dans l’application ; l’interface demande de supprimer l’ancien brouillon.

## 10. Démonstration conseillée

Démarrer avec `start.bat`, rester en Démonstration. Ouvrir Clients → Client test
visio → DEMO-VISIO-001. Montrer la facture et sa relance validée, puis le test Gmail.
Pour illustrer un paiement reçu hors logiciel, marquer la facture payée : le reste
dû disparaît et la relance est retirée. Pour rejouer, corriger en impayée puis
préparer et valider une nouvelle relance. Les messages restent modifiables
avant validation avec le bouton existant « Modifier le message ».

READY FOR DEMO WITH MINOR ISSUES

Le parcours de visio sur ordinateur fonctionne, y compris la copie Gmail réelle
en brouillon. Aucun envoi n’est activé ; la nouvelle revue visuelle mobile reste à terminer.
