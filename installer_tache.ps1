# Cree la tache planifiee Windows qui lance les quetes Microsoft Rewards chaque jour.
# - tous les jours a 13h00 (rattrapee au demarrage si le PC etait eteint)
# - et 5 minutes apres chaque ouverture de session
# Le script ne refait rien si tout est deja fait, donc plusieurs lancements par jour sont sans risque.
param([string]$Heure = "13:00")

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $dir ".venv\Scripts\pythonw.exe"   # pythonw : aucune fenetre de console
$user = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$dir\rewards.py`"" -WorkingDirectory $dir
$daily = New-ScheduledTaskTrigger -Daily -At $Heure
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
$logon.Delay = "PT5M"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
# Interactive : la tache tourne dans ta session (necessaire pour Edge et les bureaux virtuels).
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "Microsoft Rewards auto" -Action $action -Trigger $daily, $logon `
    -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Tache 'Microsoft Rewards auto' installee : tous les jours a $Heure et a l'ouverture de session."
