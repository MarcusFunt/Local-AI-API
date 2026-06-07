' 1. Commit + push the evaluation system
' 2. Run voice evaluation layer 1 inside the gateway container
' 3. Save results to evaluation/reports/
Set oShell = CreateObject("WScript.Shell")
Set oFSO   = CreateObject("Scripting.FileSystemObject")
root    = "C:\Users\marcu\OneDrive\Dokumenter\GitHub\Local-AI-API"
logPath = root & "\logs\eval-run.log"

' Step 1: commit + push
cmd1 = "cmd /c (" & _
    "git -C """ & root & """ add evaluation tests/test_voice_roundtrip_smoke.py" & _
    " && git -C """ & root & """ commit -m ""Add voice round-trip evaluation system (3-layer WER/CER/Jaccard)""" & _
    " && git -C """ & root & """ push origin main" & _
    ") > """ & logPath & """ 2>&1"
oShell.Run cmd1, 0, True

' Step 2: run layer 1 inside the running gateway container
'   --runs-dir /app/evaluation/reports/runs  maps to the bind-mounted host dir
cmd2 = "cmd /c docker exec local-ai-api-gateway-1 python /app/evaluation/voice_roundtrip.py" & _
    " --layer 1" & _
    " --runs-dir /app/evaluation/reports/runs" & _
    " --output-dir /app/evaluation/reports" & _
    " >> """ & logPath & """ 2>&1"
oShell.Run cmd2, 0, True

Set f = oFSO.OpenTextFile(logPath, 8, True)
f.WriteLine ""
f.WriteLine "=== done " & Now() & " ==="
f.Close
