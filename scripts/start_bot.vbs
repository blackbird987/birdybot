' Start Claude Bot silently (no console window at all)
' Place a shortcut to this file in shell:startup for auto-start

Set fso = CreateObject("Scripting.FileSystemObject")
botDir = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))

Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = botDir
WshShell.Run "C:\Python312\pythonw.exe -c ""import os; os.chdir(r'" & botDir & "'); import runpy; runpy.run_module('bot', run_name='__main__')"" ", 0, False
