# Disk usage and cleanup

Local models are large, so the Docker stack can use tens of gigabytes. This page
covers how much space to expect, how to check usage, and how to reclaim it —
including the Windows/WSL2 case where Docker's virtual disk grows and never
shrinks on its own.

## How much space to expect

| Item | Approx. size |
|---|---|
| `ollama/ollama` image | ~8–10 GB |
| Gateway image (Low Compute Mode / CPU-only PyTorch) | ~4.8 GB |
| Gateway image (default, CUDA PyTorch) | ~11.5 GB |
| Agent Zero image | several GB |
| `qwen3.5:0.8b` (`dev`) | ~1.0 GB |
| `qwen3.5:4b` (`small`) | ~3.4 GB |
| `qwen3.5:9b` (`main`) | ~6.6 GB |
| `qwen3:8b` (`agent-utility`) | ~5.2 GB |
| `qwen3:14b` (`agent`) | ~9.3 GB |
| All five default models | ~25.5 GB |
| Whisper + Chatterbox model cache | ~1–2 GB |

Rough totals:

- **Default full stack (all five models + Agent Zero + audio):** ~50–60 GB.
- **Low Compute Mode (`qwen3.5:0.8b` only, CPU-only image, audio):** ~15–20 GB.

If you are tight on space, use [Low Compute Mode](../README.md#low-compute-mode)
or the graphical installer's model selector to pull only what you need.

## Checking usage

```bash
docker system df                       # images, containers, volumes, build cache
docker exec local-ai-api-ollama-1 ollama list   # pulled models and their sizes
```

## Reclaiming space (Docker responsive)

Remove models you don't use (they re-download on demand):

```bash
docker exec local-ai-api-ollama-1 ollama rm qwen3:14b qwen3:8b
```

Remove dangling build cache and unused images:

```bash
docker builder prune -f
docker image prune -f
```

> `docker system prune -af` also removes the `ollama/ollama` image and any image
> not attached to a running container, forcing a re-pull. Prefer the targeted
> commands above unless you want a clean slate.

Removing data from inside Docker frees space **inside** its virtual disk but does
not shrink the disk file itself. On Windows, see the next section.

## Windows / WSL2: the disk that never shrinks

Docker Desktop stores everything in a single WSL2 virtual disk, by default at:

```
%LOCALAPPDATA%\Docker\wsl\disk\docker_data.vhdx
```

This file **grows as you add data but does not shrink when you delete it** — it
can balloon far beyond the actual content (e.g. 149 GB holding ~50 GB), which can
fill your C: drive and, once the disk is full, wedge the Docker daemon so even
`docker ps` hangs.

Check the file's size in PowerShell:

```powershell
(Get-Item "$env:LOCALAPPDATA\Docker\wsl\disk\docker_data.vhdx").Length / 1GB
```

### Option A — compact in place (keeps your data)

Frees the reclaimable space **only if** the free blocks inside the disk have been
trimmed; otherwise it may reclaim little (WSL2 does not always trim). Run in an
**elevated** PowerShell after quitting Docker Desktop:

```powershell
wsl --shutdown
Start-Sleep -Seconds 8
$vhdx = "$env:LOCALAPPDATA\Docker\wsl\disk\docker_data.vhdx"
@"
select vdisk file="$vhdx"
attach vdisk readonly
compact vdisk
detach vdisk
exit
"@ | diskpart
```

Then restart Docker Desktop.

### Option B — reset Docker's data (fastest, destructive)

Deletes **all** Docker images, containers, and volumes (everything re-downloads
on the next install). This reliably reclaims the whole file. Quit Docker Desktop,
then:

```powershell
wsl --shutdown
Remove-Item "$env:LOCALAPPDATA\Docker\wsl\disk\docker_data.vhdx"
```

Restart Docker Desktop; it recreates a fresh, small data disk. This is equivalent
to Docker Desktop → **Troubleshoot → Clean / Purge data**.

### Moving Docker's disk to another drive

If your C: drive is chronically full, point Docker at a roomier drive via
**Settings → Resources → Advanced → Disk image location**, then **Apply &
restart**. Make sure the target drive has room for the whole stack (see the
sizing table above) before switching.

## Linux cleanup

The model and cache data live in Docker volumes (`ollama-data`,
`gateway-model-cache`). Use the same `ollama rm` / `docker builder prune`
commands as above. To inspect volume usage:

```bash
docker system df -v
```
