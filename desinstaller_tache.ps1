# Supprime la tache planifiee Microsoft Rewards.
Unregister-ScheduledTask -TaskName "Microsoft Rewards auto" -Confirm:$false
Write-Host "Tache 'Microsoft Rewards auto' supprimee."
