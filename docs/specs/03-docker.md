# Spec: Containerize Flask App

## Overview

Package the Flask app into a Docker image using a slim Python base so the
build is reproducible on any machine and identical to what will be pushed to
ECR in Feature 4. The container must run as a non-root user, emit only
structured JSON to stdout, and respond on `/health` when run locally — the
same requirements the EC2 host will need.

## Requirements

1. `Dockerfile` uses `python:3.12-slim` as the base image.
2. Dependencies are installed from `requirements.txt` before copying app code
   so the pip-install layer is cached when only `app.py` changes.
3. The app process runs as a non-root user (`appuser`) created with
   `useradd -m appuser`.
4. The container listens on port 8000 (`EXPOSE 8000`; `app.run` already binds
   `0.0.0.0:8000`).
5. `CMD` uses the JSON exec form (`["python", "app.py"]`) so `SIGTERM` reaches
   the Python process directly, not a shell wrapper.
6. `.dockerignore` excludes: `.git`, `__pycache__`, `infra/`, `*.tfstate`,
   `.env`, `docs/`, `sentinel-build-plan.md`, `*.md`, `.DS_Store`.
7. All container stdout is valid JSON (the `click.echo` redirect in Feature 2
   keeps Flask's startup banner off stdout).
8. Running without `HEARTBEAT_ENABLED=true` requires no AWS credentials
   (boto3 is never imported at startup).

## Out of scope

- ECR push, EC2 deployment, and GitHub Actions CI/CD (Feature 4).
- The `awslogs` Docker log driver configuration (Feature 4).
- Gunicorn or any production WSGI server — `python app.py` only.
- Multi-stage builds or image size optimization beyond using the slim base.

## Acceptance criteria

- `docker build -t sentinel .` completes with exit code 0 and no errors.
- `docker run -p 8000:8000 sentinel` starts successfully; `GET /health`
  returns HTTP 200 with JSON body `{"status": "healthy"}`.
- `GET /` returns HTTP 200 with JSON body containing `"status": "ok"`.
- `docker logs <container>` shows only valid JSON lines; `docker logs
  <container> | jq .` produces no parse errors.
- `docker exec <container> ps aux` shows the app process owned by `appuser`,
  not `root`.
- `docker run --rm -p 8000:8000 sentinel` exits cleanly (no error) on
  `docker stop` (i.e. `SIGTERM` propagates to Python and the process exits
  within the default 10-second grace period).
- `docker build` with a `.env` file present in the repo root does **not**
  include `.env` in the image. Verify with:
  `docker run --rm sentinel sh -c "ls /app/.env 2>&1 || echo 'confirmed absent'"`
  — output must be `confirmed absent`.
- Same check for `*.tfstate`:
  `docker run --rm sentinel sh -c "ls /app/*.tfstate 2>&1 || echo 'confirmed absent'"`
  — output must be `confirmed absent`.
- Running without any `AWS_*` environment variables set does not produce an
  AWS credential error during startup (heartbeat is disabled by default).
- `GET /simulate-failure?mode=error` inside the container returns 200 and
  produces 5 `"level": "ERROR"` JSON lines in `docker logs`.
- `GET /simulate-failure?mode=crash` causes the container to stop with a
  non-zero exit code (`docker inspect` shows `"ExitCode": 1`).

## Notes

- Layer order matters: `COPY requirements.txt .` → `RUN pip install` →
  `COPY app.py .` avoids re-running pip on every code change.
- Use `--no-cache-dir` on `pip install` to keep the image smaller.
- `WORKDIR /app` before any `COPY` keeps paths predictable and avoids writing
  to `/`.
- `useradd -m appuser` creates a home directory; pair with `USER appuser`
  after installing deps (installing as root is fine; running as root is not).
- `CMD ["python", "app.py"]` (exec form) is required — shell form
  (`CMD python app.py`) launches a `sh -c` wrapper that holds PID 1 and
  swallows `SIGTERM`, preventing clean shutdown.
- `EXPOSE 8000` is documentation only; the actual port binding is `-p 8000:8000`
  on `docker run`. Include it anyway — Feature 4's EC2 user-data script will
  reference the port.
- No IAM permissions needed for this feature. The heartbeat is disabled by
  default; boto3 is imported lazily only when `HEARTBEAT_ENABLED=true`.
- `.gitignore` already excludes `.env` and `*.tfstate`; `.dockerignore` must
  list them independently — Docker does not read `.gitignore`.
