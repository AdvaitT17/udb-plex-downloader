"""Background worker: pulls queued jobs and invokes UDB serially."""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import json
import urllib.request
from pathlib import Path
from typing import Any

from .db import JobDB


class Worker:
    def __init__(
        self,
        db: JobDB,
        udb_root: Path,
        config_file: Path,
        log_dir: Path,
        python_bin: str = "python3",
        plex_refresh_url: str | None = None,
        download_root: Path | None = None,
    ):
        self.db = db
        self.udb_root = udb_root
        self.config_file = config_file
        self.log_dir = log_dir
        self.python_bin = python_bin
        self.plex_refresh_url = plex_refresh_url
        self.download_root = download_root
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        # job_id -> subprocess.Popen of the currently running udb proc.
        # Used by cancel_job() to terminate a running job.
        self._running: dict[int, subprocess.Popen] = {}
        self._running_lock = threading.Lock()
        # Flag per job_id indicating explicit cancellation, so _run_job
        # records the reason correctly.
        self._cancelled: set[int] = set()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def cancel_job(self, job_id: int) -> bool:
        """Kill the subprocess for job_id if running. Returns True if killed."""
        with self._running_lock:
            proc = self._running.get(job_id)
            if proc is None:
                return False
            self._cancelled.add(job_id)
        try:
            proc.terminate()
        except Exception:
            pass
        # Wait up to 3s then hard-kill
        for _ in range(15):
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            row = self.db.claim_next()
            if row is None:
                time.sleep(2)
                continue
            try:
                self._run_job(row)
            except Exception as e:  # noqa: BLE001
                self.db.finish(row["id"], 1, error=str(e))

    def _run_job(self, row: dict[str, Any]) -> None:
        job_id = row["id"]
        payload_raw = row["payload"]
        payload = payload_raw if isinstance(payload_raw, dict) else json.loads(payload_raw)

        cmd = self._build_cmd(payload)
        log_path = self.log_dir / f"job_{job_id}.log"
        self.db.set_log_path(job_id, str(log_path))

        # Pre-seed symlinks from udb's expected filenames to the Plex-renamed
        # files already on disk, so udb's skip-existing check fires and we
        # don't re-download episodes we already have. Symlinks get cleaned
        # up post-run (or at least left pointing at the real file; they
        # remain valid).
        seeded_symlinks: list[Path] = []
        if self.download_root is not None:
            try:
                seeded_symlinks = self._seed_skip_symlinks(payload, self.download_root)
            except Exception as e:
                append_to_log(log_path, f"[seed] failed: {e}\n")

        # udb reads from stdin only when it drops into an interactive prompt
        # (e.g. ambiguous search results without a predefined year). stdin is
        # closed below so the read raises EOFError immediately — but udb still
        # emits a visible "Select one of …" line first and may idle briefly
        # before its top-level handler trips. We tail the log in a sibling
        # thread and kill the proc as soon as a prompt marker appears so a
        # single bad payload can't tie up the worker.
        prompt_markers = (
            "Select one of the above",
            "Enter series/movie name",
            "Enter seasons to download",
            "Enter episodes to download",
            "Enter download resolution",
            "Download entire season",
        )
        prompt_hit: dict[str, str | None] = {"marker": None}

        def _tail_for_prompts(path: Path, stop: threading.Event, proc_ref: dict[str, Any]) -> None:
            # Poll the log for prompt markers; if one shows up, kill udb fast.
            seen = 0
            while not stop.is_set():
                try:
                    data = path.read_text(errors="replace")
                except FileNotFoundError:
                    time.sleep(0.2)
                    continue
                if len(data) > seen:
                    chunk = data[seen:]
                    seen = len(data)
                    for m in prompt_markers:
                        if m in chunk:
                            prompt_hit["marker"] = m
                            p = proc_ref.get("proc")
                            if p and p.poll() is None:
                                try:
                                    p.terminate()
                                except Exception:
                                    pass
                                # hard kill after a short grace period
                                for _ in range(10):
                                    if p.poll() is not None:
                                        break
                                    time.sleep(0.2)
                                if p.poll() is None:
                                    try:
                                        p.kill()
                                    except Exception:
                                        pass
                            return
                time.sleep(0.3)

        with open(log_path, "wb") as log_f:
            log_f.write(f"$ {' '.join(cmd)}\n".encode())
            log_f.flush()
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.udb_root),
                stdout=log_f,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env={
                    **os.environ,
                    "PYTHONUNBUFFERED": "1",
                    # udb calls os.get_terminal_size() at the end to draw a
                    # summary divider. With stdout redirected to a file the
                    # underlying ioctl raises ENOTTY; Python honors COLUMNS as
                    # a fallback before issuing the syscall.
                    "COLUMNS": "120",
                    "LINES": "40",
                },
            )
            proc_ref: dict[str, Any] = {"proc": proc}
            with self._running_lock:
                self._running[job_id] = proc
            stop_tail = threading.Event()
            tailer = threading.Thread(
                target=_tail_for_prompts,
                args=(log_path, stop_tail, proc_ref),
                daemon=True,
            )
            tailer.start()
            try:
                exit_code = proc.wait()
            finally:
                stop_tail.set()
                tailer.join(timeout=1.0)
                with self._running_lock:
                    self._running.pop(job_id, None)

        cancelled = job_id in self._cancelled
        self._cancelled.discard(job_id)

        # UDB returns 0 even on hard errors (e.g. Cloudflare 429 during episode
        # fetch). Scan the log for known error markers and override the status.
        error: str | None = None
        if cancelled:
            error = "Cancelled by user"
            if exit_code == 0:
                exit_code = 130  # convention: SIGINT-ish
        elif prompt_hit["marker"]:
            error = (
                f"udb hit an interactive prompt ({prompt_hit['marker']!r}) — "
                "payload was probably missing year/episodes. Job killed."
            )
            if exit_code == 0:
                exit_code = 1
        try:
            tail = log_path.read_text(errors="replace").splitlines()[-60:]
        except Exception:
            tail = []
        error_markers = (
            "Failed with code:",
            "Error occurred:",
            "No episodes are available",
            "No episodes available to download",
            "No episodes found",
            "Download halted",
            "Traceback (most recent call last):",
        )
        for line in tail:
            if any(m in line for m in error_markers):
                if error is None:
                    error = line.strip()
                if exit_code == 0:
                    exit_code = 1
                break
        if error is None and exit_code != 0:
            error = f"udb exited with code {exit_code}"

        # Tear down the skip-check symlinks we seeded before the run so the
        # series dir only contains real files (Plex, rsync, etc. prefer that).
        if seeded_symlinks:
            for link in seeded_symlinks:
                try:
                    if link.is_symlink():
                        link.unlink()
                except Exception:
                    pass

        # On success, rename files into Plex's preferred format so the TV
        # scanner matches episodes correctly. udb writes e.g.
        #   "Sold Out on You Episode 5 - 640P.mp4"
        # Plex wants something like
        #   "Sold Out on You - s01e05.mp4"
        renamed: list[str] = []
        if exit_code == 0 and self.download_root is not None:
            try:
                renamed = self._plexify_filenames(payload, self.download_root)
            except Exception as e:  # don't let a rename bug fail the whole job
                renamed = []
                append_to_log(log_path, f"\n[rename] failed: {e}\n")
        if renamed:
            append_to_log(log_path, "\n[rename] " + "\n[rename] ".join(renamed) + "\n")

        self.db.finish(job_id, exit_code, error=error)

        if exit_code == 0 and self.plex_refresh_url:
            try:
                urllib.request.urlopen(self.plex_refresh_url, timeout=10).read()
            except Exception:
                pass

    def _build_cmd(self, payload: dict[str, Any]) -> list[str]:
        cmd = [
            self.python_bin,
            "udb.py",
            "-c", str(self.config_file),
            "-d",      # start download immediately
            "-dl",     # disable looping
            "-dc",     # disable colors
        ]
        # series type: default to 2 (KissKh)
        series_type = int(payload.get("series_type", 2))
        cmd += ["-s", str(series_type)]

        name = payload.get("name")
        if not name:
            raise ValueError("payload.name is required")
        cmd += ["-n", str(name)]

        if payload.get("year"):
            cmd += ["-y", str(int(payload["year"]))]
        for season in payload.get("seasons") or []:
            cmd += ["-S", str(season)]
        episodes = payload.get("episodes") or []
        if not episodes:
            # UDB requires an episode range otherwise it prompts interactively.
            # Use a big range; UDB clamps it to the actual episode count.
            episodes = ["1-999"]
        for ep in episodes:
            cmd += ["-e", str(ep)]
        if payload.get("resolution"):
            cmd += ["-r", str(payload["resolution"])]

        return cmd

    # --- filename normalization --------------------------------------------
    # udb writes "Show Name Episode N - <res>P.mp4" (or "Show Name Movie -
    # <res>P.mp4"). Plex's TV scanner matches files like
    # "Show Name - s01e05.mp4". We do a best-effort in-place rename. KissKh
    # has no season concept per series so we always use s01.
    _UDB_EP_RE = re.compile(
        r"^(?P<show>.+?)\s+Episode\s+(?P<ep>\d+(?:\.\d+)?)(?:\s+-\s+\d+P)?\.(?P<ext>mp4|mkv|ts)$",
        re.IGNORECASE,
    )
    _UDB_MOVIE_RE = re.compile(
        r"^(?P<show>.+?)\s+Movie(?:\s+-\s+\d+P)?\.(?P<ext>mp4|mkv|ts)$",
        re.IGNORECASE,
    )
    # Reverse: match files already in Plex format, so we can symlink them
    # back to udb's expected name before a rescan.
    _PLEX_EP_RE = re.compile(
        r"^(?P<show>.+?)\s+-\s+s(?P<s>\d{2})e(?P<e>\d{2,3})\.(?P<ext>mp4|mkv|ts)$",
        re.IGNORECASE,
    )

    def _resolve_series_dir(self, payload: dict[str, Any], download_root: Path) -> Path | None:
        name = str(payload.get("name") or "").strip()
        if not name:
            return None
        year = payload.get("year")
        candidates = []
        if year:
            candidates.append(download_root / f"{name} ({year})")
        candidates.append(download_root / name)
        return next((c for c in candidates if c.is_dir()), None)

    def _seed_skip_symlinks(
        self, payload: dict[str, Any], download_root: Path
    ) -> list[Path]:
        """For each Plex-format file already on disk, create a symlink with
        udb's expected filename so udb skips re-downloading it. Returns the
        list of created symlink paths for later cleanup."""
        series_dir = self._resolve_series_dir(payload, download_root)
        if series_dir is None:
            return []
        resolution = str(payload.get("resolution") or "720")
        created: list[Path] = []
        for p in sorted(series_dir.iterdir()):
            if not p.is_file() or p.is_symlink():
                continue
            m = self._PLEX_EP_RE.match(p.name)
            if not m:
                continue
            show = m.group("show").strip()
            ep = int(m.group("e"))
            ext = m.group("ext").lower()
            # Try both the current resolution and a few common fallbacks;
            # udb's skip check uses the filename it's about to write, which
            # depends on the resolution it picks. Seeding all plausible ones
            # is cheap (symlinks are free).
            for res in {resolution, "720", "640", "540", "480", "360", "1080"}:
                link_name = f"{show} Episode {ep} - {res}P.{ext}"
                link_path = series_dir / link_name
                if link_path.exists() or link_path.is_symlink():
                    continue
                try:
                    link_path.symlink_to(p.name)  # relative link within the dir
                    created.append(link_path)
                except OSError:
                    pass
        return created

    def _plexify_filenames(self, payload: dict[str, Any], download_root: Path) -> list[str]:
        """Rename freshly-downloaded files in-place. Returns list of
        'old -> new' strings for logging."""
        series_dir = self._resolve_series_dir(payload, download_root)
        if series_dir is None:
            return []

        results: list[str] = []
        for p in sorted(series_dir.iterdir()):
            # Skip symlinks (seeded ones were already cleaned up, but belt-and-
            # suspenders) — only rename real files.
            if not p.is_file() or p.is_symlink():
                continue
            new_stem: str | None = None
            ext = p.suffix.lstrip(".").lower() or "mp4"
            m = self._UDB_EP_RE.match(p.name)
            if m:
                show = m.group("show").strip()
                ep_num = int(float(m.group("ep")))
                new_stem = f"{show} - s01e{ep_num:02d}"
            else:
                mm = self._UDB_MOVIE_RE.match(p.name)
                if mm:
                    show = mm.group("show").strip()
                    new_stem = show  # Plex movie-agent uses just the title
            if not new_stem:
                continue
            new_name = f"{new_stem}.{ext}"
            if new_name == p.name:
                continue
            new_path = p.with_name(new_name)
            if new_path.exists():
                # Don't clobber (udb's own skip-existing would have prevented
                # the re-download, but guard anyway).
                continue
            try:
                p.rename(new_path)
                results.append(f"{p.name} -> {new_name}")
            except OSError as e:
                results.append(f"{p.name} -> !! {e}")
        return results


def append_to_log(path: Path, text: str) -> None:
    try:
        with open(path, "a") as f:
            f.write(text)
    except Exception:
        pass
