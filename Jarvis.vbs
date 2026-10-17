' Запуск Джарвиса без консольного окна (двойной клик). Сторож перезапускает после сбоя.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
py = dir & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(py) Then py = dir & "\python\pythonw.exe"
args = ""
If WScript.Arguments.Count > 0 Then args = " " & WScript.Arguments(0)
sh.Run """" & py & """ -m jarvis.watchdog" & args, 0, False
