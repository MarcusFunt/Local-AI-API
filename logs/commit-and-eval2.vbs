' Fix: commit all new files, then run evaluation inside the gateway container.
Set oShell = CreateObject("WScript.Shell")
Set oFSO   = CreateObject("Scripting.FileSystemObject")
root    = "C:\Users\marcu\OneDrive\Dokumenter\GitHub\Local-AI-API"
logPath = root & "\logs\eval-run2.log"

' Step 1: Commit and push all new work
Dim cmd1
cmd1 = "cmd /c (" & _
    "git -C """ & root & """ add -A" & _
    " && git -C """ & root & """ commit -m ""Add RAG pipeline (LlamaIndex+Qdrant), FastMCP server, voice evaluation""" & _
    " && git -C """ & root & """ push origin main" & _
    ") > """ & logPath & """ 2>&1"
oShell.Run cmd1, 0, True

' Step 2: Find the gateway container name
Dim cmd2
cmd2 = "cmd /c docker ps --filter name=gateway --format ""{{.Names}}"" >> """ & logPath & """ 2>&1"
oShell.Run cmd2, 0, True

' Step 3: Run evaluation layer 1 inside the gateway container
' Use docker exec with the container found above
Dim cmd3
cmd3 = "cmd /c (for /f ""tokens=*"" %%C in ('docker ps --filter name=gateway --filter status=running --format ""{{.Names}}""') do " & _
    "docker exec %%C python /app/evaluation/voice_roundtrip.py " & _
    "--layer 1 " & _
    "--runs-dir /app/evaluation/reports/runs " & _
    "--output-dir /app/evaluation/reports" & _
    ") >> """ & logPath & """ 2>&1"
oShell.Run cmd3, 0, True

Set f = oFSO.OpenTextFile(logPath, 8, True)
f.WriteLine ""
f.WriteLine "=== done " & Now() & " ==="
f.Close
