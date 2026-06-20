' Simple: git add -A, commit, push. Uses PowerShell for reliability.
Set oShell = CreateObject("WScript.Shell")
root = "C:\Users\marcu\OneDrive\Dokumenter\GitHub\Local-AI-API"
log  = root & "\logs\commit-all.log"

Dim ps
ps = "powershell -NoProfile -Command """ & _
     "Set-Location '" & root & "'; " & _
     "git add -A; " & _
     "git commit -m 'Add RAG pipeline, FastMCP server, voice evaluation system'; " & _
     "git push origin main | Out-String" & _
     """ > """ & log & """ 2>&1"

oShell.Run ps, 0, True

Set oFSO = CreateObject("Scripting.FileSystemObject")
Set f = oFSO.OpenTextFile(log, 8, True)
f.WriteLine "=== done " & Now() & " ==="
f.Close
