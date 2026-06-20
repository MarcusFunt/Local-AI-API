@echo off
cd /d "C:\Users\marcu\OneDrive\Dokumenter\GitHub\Local-AI-API"
powershell -NoProfile -Command "git add -A; git commit -m 'Add RAG pipeline, FastMCP server, voice evaluation system'; git push origin main" > logs\commit-all.log 2>&1
echo Done >> logs\commit-all.log
