# Cree la tache planifiee Windows qui lance les quetes Microsoft Rewards chaque jour.
# - tous les jours a 13h00 et a 18h00 (rattrapees au demarrage si le PC etait eteint) : certaines
#   activites ne sont publiees qu'en cours de journee, le second passage les recupere
# - et 5 minutes apres chaque ouverture de session
# Le script ne refait rien si tout est deja fait, donc plusieurs lancements par jour sont sans risque.
param([string]$Heure = "13:00", [string]$Heure2 = "18:00")

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $dir ".venv\Scripts\pythonw.exe"   # pythonw : aucune fenetre de console
$user = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$dir\rewards.py`"" -WorkingDirectory $dir
$daily = New-ScheduledTaskTrigger -Daily -At $Heure
$daily2 = New-ScheduledTaskTrigger -Daily -At $Heure2
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
$logon.Delay = "PT5M"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
# Interactive : la tache tourne dans ta session (necessaire pour Edge et les bureaux virtuels).
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "Microsoft Rewards auto" -Action $action -Trigger $daily, $daily2, $logon `
    -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Tache 'Microsoft Rewards auto' installee : tous les jours a $Heure et $Heure2, et a l'ouverture de session."
