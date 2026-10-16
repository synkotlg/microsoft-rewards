# Microsoft Rewards automatique

Fait chaque jour les quêtes de https://rewards.bing.com dans Microsoft Edge, sur un
**bureau virtuel caché** (« Microsoft Rewards », créé puis supprimé automatiquement) :
tu peux utiliser ton PC normalement pendant ce temps.

## Ce que fait le script

| Quête | Détail |
|---|---|
| Sections du tableau de bord | déplie les sections (+5 points la première fois) |
| Ensemble du jour | les 3 défis quotidiens (liens, quiz : la bonne réponse est lue dans la page) |
| Autres activités | « Continuer à gagner » : quiz, liens Bing, et offres promotionnelles qui rapportent des points (ex. Bing Image Creator +20) |
| Recherches thématiques | « Explorer sur Bing » : active la carte puis fait une recherche **concrète** sur le thème (une vraie chanson pour « paroles », une vraie ville pour « l'heure »…), sinon Microsoft ne valide pas |
| Quêtes | tâches débloquées des cartes à tampons (onboarding, tendances du mois, etc.) |
| Recherches Bing | jusqu'au plafond du jour (15/30/60 points selon ton niveau), requêtes tirées des tendances Google |
| Points à réclamer | clique « Prêt à réclamer » : ces points expirent au bout d'un mois s'ils ne sont pas réclamés |

Après chaque série, le script **relit la page pour vérifier ce que Microsoft a vraiment validé**
(✓ dans le journal), et réessaie une fois (avec une autre requête pour les recherches thématiques).
Une offre restée non validée deux jours différents (✗) est mise de côté 7 jours dans `state.json` :
le script ne reclique plus chaque jour une tâche que Microsoft ne valide jamais. Supprimer
`state.json` remet tout à zéro.

Jamais fait : échanger des points, installer une application, télécharger un fichier, parrainer,
ouvrir un lien vers une application Windows (`ms-search:`, Microsoft Store). Les offres dont le lien
ou le texte l'indique (« Télécharger… », « Installer… », « Parrainez… ») sont ignorées, et les
téléchargements sont de toute façon bloqués dans le navigateur.

Non fait : les tâches de l'application mobile Bing (check-in, « lire pour gagner », séries de
l'application). Sur PC, elles ne sont faisables qu'en imitant l'application auprès des serveurs de
Microsoft, ce qui est la méthode la plus repérée. Elles prennent une minute à la main sur le téléphone.

## Arrêt automatique si Microsoft signale le compte

Si une page Rewards contient un avertissement de fraude (`Fraud_UserWarning…`) ou affiche
« Votre compte Microsoft Rewards a été suspendu », le script **s'arrête aussitôt sans rien faire**
et affiche une alerte. Il fera de même à chaque lancement tant que l'avertissement sera présent.
C'est le moment d'arrêter l'automatisation (`desinstaller_tache.ps1`) plutôt que d'insister.

## Installation

1. Double-clique sur **`setup.bat`** : Edge s'ouvre, connecte-toi à ton compte Microsoft
   (coche « Rester connecté »), puis ferme la fenêtre. La connexion est gardée dans `edge-profile/`
   (ton Edge habituel n'est pas touché).
2. Planifie l'exécution quotidienne (dans PowerShell, depuis ce dossier) :
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\installer_tache.ps1            # 13h00 par défaut
   powershell -ExecutionPolicy Bypass -File .\installer_tache.ps1 -Heure 09:30
   ```
   La tâche tourne aussi 5 min après chaque ouverture de session, et elle est rattrapée si le PC
   était éteint à l'heure prévue.

## Utilisation

- `lancer.bat` : lance les quêtes maintenant.
- `lancer.bat --visible` : même chose mais avec la fenêtre Edge visible (pour voir ce qui se passe).
- `lancer.bat --no-search` : sans les recherches Bing.
- Journaux : `logs/AAAA-MM-JJ.log` (30 jours conservés). `→` = carte cliquée, `✓` = validée par
  Microsoft, `✗` = non validée, `?` = état inconnu (la page rechargée était incomplète).
- Si la session expire, une fenêtre d'alerte le signale : relance `setup.bat`.
- Pour arrêter l'automatisation : `desinstaller_tache.ps1`.

## Si Microsoft change son site

`lancer.bat --dump` enregistre les pages et les offres détectées dans `dump/` pour comprendre
ce qui a changé. Le script lit les données embarquées par la page (flux Next.js `self.__next_f`)
et clique sur les vraies cartes : c'est le site qui déclare l'activité, comme lors d'un clic manuel.

> ⚠️ L'automatisation est contraire au règlement de Microsoft Rewards, qui peut suspendre un compte
> et annuler ses points. Le script ne cherche pas à cacher qu'il est automatisé : Microsoft peut le
> détecter. Ce qui limite le risque : un seul compte par foyer, pas de VPN ni de proxy, ne pas
> dépasser les plafonds du jour, et s'arrêter dès le premier avertissement (ce que fait le script).
