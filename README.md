# Omi -> Jetson Thor local pipeline (Phase 0-3)

This covers: finding where the phone stores recordings, syncing them to the
Thor, and automatically transcribing them with faster-whisper. LLM analysis
(Phase 4) and the report/email step (Phase 5) come next, once transcripts
are flowing reliably.

## Directory layout on the Thor

Two separate locations, kept apart deliberately — code in one place, runtime
data (audio, transcripts, logs) in another. Mixing them causes real problems
later: Docker's build context would try to include gigabytes of audio if
data lived inside the repo, and a `git status` full of audio files/transcripts
is miserable to work with.

```
/home/efranklin/
├── projects/
│   └── omi-pipeline/          <- CODE (this repo's contents go here)
│       ├── config.py
│       ├── watcher.py
│       ├── transcribe.py
│       ├── requirements.txt
│       ├── omi-watcher.service
│       ├── .dockerignore
│       ├── .env                <- you create this (copy from docker/.env.example)
│       └── docker/
│           ├── Dockerfile
│           ├── docker-compose.yml
│           ├── entrypoint.sh
│           ├── requirements-docker.txt
│           ├── .env.example
│           └── host-setup/
│               ├── enable-mps.sh
│               └── nvidia-mps.service
│
└── omi-data/                   <- DATA (created fresh, not part of this repo)
    ├── inbox/                  <- Syncthing drops files here
    ├── processing/
    ├── archive/
    ├── transcripts/
    ├── failed/
    └── pipeline.log
```

Why `~/projects/omi-pipeline` rather than directly in `~`: keeps your home
directory tidy as you inevitably add other projects (robotics work, etc.)
alongside this one. Use whatever parent directory convention you already use
for other code on this machine — nothing here depends on that specific path,
just keep it consistent with what's in `.env` / `omi-watcher.service`.

### Getting the files onto the Thor

You've got these files sitting wherever you downloaded them from our
conversation so far. A few ways to get them onto the Thor, roughly in order
of how much you'll want this going forward:

**Quick and dirty (fine for right now):**
```bash
# From whatever machine has the files, replace with the Thor's actual address
scp -r ./omi-pipeline efranklin@<thor-ip>:~/projects/
```

**Better for the long run — put it in git.** You're going to be iterating on
this a lot (Phase 4/5 still to come, plus tuning once real Omi data flows),
and you're already living in GitLab daily — a quick private personal project
there gets you history, easy diffing as we adjust the Dockerfile/prompts, and
a trivial `git pull` on the Thor instead of re-copying files each time:

```bash
# One-time, wherever you're set up to push
cd omi-pipeline && git init && git add . && git commit -m "initial pipeline scaffold"
git remote add origin <your-gitlab-repo-url>
git push -u origin main

# On the Thor
git clone <your-gitlab-repo-url> ~/projects/omi-pipeline
```

Either way, once the code's in place:

```bash
mkdir -p ~/omi-data/{inbox,processing,archive,transcripts,failed}
cd ~/projects/omi-pipeline
cp docker/.env.example .env
# edit .env — set OMI_DATA_DIR=/home/efranklin/omi-data (or wherever you put it)
```

## Phase 0 — Find the recordings on the phone

With the phone on USB (or `adb connect <phone-ip>:5555` over Wi-Fi, USB
debugging enabled):

```bash
adb shell pm list packages | grep -i omi
adb shell pm path <package.name>

# App-private storage (works if the app is debuggable, or phone is rooted)
adb shell run-as <package.name> ls -la /data/data/<package.name>/app_flutter/
adb shell run-as <package.name> ls -la /data/data/<package.name>/files/

# Shared/external storage (path_provider sometimes lands here instead)
adb shell ls -la /sdcard/Android/data/<package.name>/files/
adb shell find /sdcard -iname "*.wav" -o -iname "*.opus" -o -iname "*.m4a" 2>/dev/null
```

Once you find real files, pull one and check it on the Thor:

```bash
adb pull /sdcard/Android/data/<package.name>/files/some_recording.wav .
ffprobe some_recording.wav
```

`ffprobe` output tells you the codec/sample rate/channels — faster-whisper
(via ffmpeg under the hood) can ingest almost anything, so this is just to
confirm nothing unusual (e.g. a proprietary container) is going on.

If `run-as` fails with a permission error and nothing useful turns up in
`/sdcard`, that's a real signal: the stock app is sandboxing recordings
tightly. At that point, switch to **omibutfree**
(https://github.com/kbdevs/omibutfree) — it's built specifically to write to
accessible local storage (SQLite + files, by design, since its whole premise
is local-first).

## Phase 1 — Syncthing (phone -> Thor)

1. Install Syncthing on the phone (Play Store: "Syncthing") and on the Thor
   (`sudo apt install syncthing` or the official binary).
2. On the phone, add a new folder pointing at the recordings directory found
   in Phase 0. Set it to **Send Only**.
3. On the Thor, add the corresponding shared folder, pointed at
   `~/omi-pipeline/inbox` (or wherever you set `OMI_INBOX_DIR`). Set it to
   **Receive Only**.
4. Pair the two devices in the Syncthing web UI (default `localhost:8384`)
   using the device ID / QR code, over your home Wi-Fi.
5. Leave both running — sync happens automatically whenever both are on the
   same network. Nothing else to trigger.

## Phase 2/3 — Watcher + transcription

```bash
cd ~/projects/omi-pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export OMI_PIPELINE_BASE=/home/efranklin/omi-data   # the DATA dir, not this repo
python watcher.py
```

First run downloads the `large-v3` faster-whisper model — expect a pause the
first time it processes a file. Adjust `config.py` (or the equivalent env
vars) if you want a smaller/faster model while iterating, e.g.:

```bash
export OMI_WHISPER_MODEL=medium
```

### Directory layout it creates under `OMI_PIPELINE_BASE` (default `~/omi-pipeline`)

| Dir            | Purpose                                              |
| -------------- | ----------------------------------------------------- |
| `inbox/`       | Syncthing drops new files here                        |
| `processing/`  | File moved here while actively being transcribed      |
| `archive/`     | Original audio, kept after successful transcription   |
| `transcripts/` | `.json` (structured) + `.txt` (readable) output       |
| `failed/`      | Anything that errored during transcription, for review |

### Running as a service

```bash
# Edit paths/user in omi-watcher.service first if they don't match your setup
sudo cp omi-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now omi-watcher
sudo journalctl -u omi-watcher -f   # tail logs
```

## Running it containerized, with GPU sharing for robotics work (recommended)

Rather than running `watcher.py` bare in a venv, the `docker/` folder wraps it
in a container that (a) isolates its CUDA/Python dependency stack from
whatever your robotics work needs, and (b) caps its GPU usage via CUDA MPS so
most of the Thor's GPU stays free — MIG was ruled out for this since current
JetPack only exposes two fixed-size slices (~8 SM / ~12 SM out of 20 total)
and requires killing the desktop session to reconfigure; MPS gives a flexible,
dynamic percentage cap instead, with no reboot required.

### One-time host setup (on the Thor itself, not in a container)

```bash
chmod +x docker/host-setup/enable-mps.sh
./docker/host-setup/enable-mps.sh
```

To make the MPS daemon persistent across reboots:

```bash
sudo cp docker/host-setup/nvidia-mps.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nvidia-mps
```

### Build and run the pipeline container

```bash
docker compose -f docker/docker-compose.yml up -d --build
docker compose -f docker/docker-compose.yml logs -f
```

The container is capped at 25% of the GPU's SMs by default
(`CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` in `docker/docker-compose.yml`) — that's
a starting guess, not a measured number. Once you've got real transcription
throughput data from Phase 0-3 testing, adjust it up or down based on how
much headroom your robotics work actually needs at the same time.

### Things to verify once you actually have the Thor in hand

The Dockerfile was written against current published specs (JetPack 7.x,
Blackwell sm_101) but a few details can only be confirmed on the real device:

- **Base image tag**: `nvcr.io/nvidia/l4t-jetpack:r38.0.0` in the Dockerfile
  is a placeholder — check `cat /etc/nv_tegra_release` on the Thor and match
  to the correct tag on NGC, or check whether `dusty-nv/jetson-containers`
  already has a Thor-tagged base by then (that project tracks new Jetson
  hardware quickly and may save you the from-source ctranslate2 build
  entirely if a prebuilt GPU wheel shows up).
- **CUDA compute architecture**: the Dockerfile sets
  `CMAKE_CUDA_ARCHITECTURES=101` for ctranslate2's build. Confirm with
  `nvidia-smi --query-gpu=compute_cap --format=csv` and adjust if it differs.
- **MPS availability**: `enable-mps.sh` will tell you immediately if
  `nvidia-cuda-mps-control` isn't present on your image.

### Running bare (no Docker) instead

The original venv-based instructions above and `omi-watcher.service` still
work if you'd rather not containerize — MPS capping still applies the same
way, you'd just export `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` in the systemd
unit's `Environment=` lines instead of in `docker-compose.yml`. Containerizing
is recommended mainly so upgrading/rebuilding the robotics stack can never
accidentally break this pipeline's Python/CUDA environment, or vice versa.

## What's next (Phase 4/5, not built yet)

- A prompt template + Ollama call that takes each `transcripts/*.json` and
  produces Omi-style structured output (title, overview, category,
  action_items, memories).
- A daily digest job (cron + `smtplib`, or push into Obsidian as markdown).

**Heads up for Phase 4**: a community report building on this exact hardware
(AGX Thor, L4T R39, JetPack 7.2) found that a prebuilt Ollama image built for
Orin (not Thor) loaded and ran fine via compatibility, but only hit ~19
tokens/sec against a ~150 t/s expectation — because it wasn't actually
compiled for Thor's architecture. When we get to Phase 4, don't just
`curl -fsSL https://ollama.com/install.sh | sh` and assume it's fast — worth
a quick throughput sanity check against expected numbers for whatever model
you pick, same as we're being careful about with ctranslate2 here.

Once you've confirmed real audio files are landing in `transcripts/` with
sane text in them, ping me and we'll build the LLM analysis stage against
your actual transcript format/quality.
