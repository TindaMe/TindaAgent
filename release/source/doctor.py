#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

PORTS_FILE_NAME = ".tinda_ports.list"


@dataclass
class CheckResult:
    level: str
    code: str
    message: str
    hint: str = ""


def _run(cmd: list[str], cwd: Path, timeout: float = 15.0) -> tuple[int | None, str]:
    try:
        cp = subprocess.run(
            cmd,
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
        )
        out = (cp.stdout or "") + (cp.stderr or "")
        return int(cp.returncode), out
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _to_windows_path(path: Path) -> str:
    if os.name == "nt":
        return str(path)
    if _is_wsl():
        rc, out = _run(["wslpath", "-w", str(path)], Path.cwd(), timeout=4)
        if rc == 0 and str(out).strip():
            return str(out).strip()
    return str(path)


def _is_wsl() -> bool:
    if str(os.environ.get("WSL_DISTRO_NAME", "")).strip():
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except Exception:
        return False


def _parse_ports_text(text: str) -> list[int]:
    out: list[int] = []
    normalized = (text or "").replace("\r", " ").replace("\n", " ").replace(",", " ").replace(";", " ")
    for token in normalized.split():
        t = token.strip()
        if not t or not t.isdigit():
            continue
        p = int(t)
        if p <= 0 or p > 65535:
            continue
        if p not in out:
            out.append(p)
    return out


def _parse_port_records_text(text: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    normalized = (text or "").replace("\r", " ").replace("\n", " ").replace(",", " ").replace(";", " ")
    for token in normalized.split():
        t = token.strip()
        if not t:
            continue
        env_tag = "legacy"
        raw_port = t
        if ":" in t:
            head, tail = t.split(":", 1)
            if head and tail:
                env_tag = head.strip().lower()
                raw_port = tail.strip()
        if not raw_port.isdigit():
            continue
        p = int(raw_port)
        if p <= 0 or p > 65535:
            continue
        if env_tag in {"win", "nt"}:
            env_tag = "windows"
        elif env_tag in {"gnu/linux"}:
            env_tag = "linux"
        elif not env_tag:
            env_tag = "legacy"
        rec = (env_tag, p)
        if rec not in out:
            out.append(rec)
    return out


def _detect_env_tag() -> str:
    if os.name == "nt":
        return "windows"
    if _is_wsl():
        return "wsl"
    return "linux"


def _read_tracked_port_records(repo_root: Path) -> list[tuple[str, int]]:
    ports_file = repo_root / PORTS_FILE_NAME
    if not ports_file.exists():
        return []
    try:
        return _parse_port_records_text(ports_file.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []


def _read_tracked_ports(repo_root: Path) -> list[int]:
    env_tag = _detect_env_tag()
    rows = _read_tracked_port_records(repo_root)
    out: list[int] = []
    for rec_env, p in rows:
        if rec_env in {"legacy", env_tag}:
            if p not in out:
                out.append(p)
    return out


def _is_port_open(host: str, port: int, timeout_sec: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout_sec):
            return True
    except Exception:
        return False


def _find_listening_ports() -> set[int]:
    ports: set[int] = set()
    if os.name == "nt":
        rc, out = _run(["cmd.exe", "/c", "netstat -ano -p tcp"], Path.cwd(), timeout=8)
        if rc is None:
            return ports
        pattern = re.compile(r"^\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+\d+\s*$", re.IGNORECASE)
        for line in out.splitlines():
            m = pattern.search(line)
            if m:
                try:
                    ports.add(int(m.group(1)))
                except Exception:
                    pass
        return ports

    if shutil.which("ss"):
        rc, out = _run(["ss", "-ltnH"], Path.cwd(), timeout=8)
        if rc is not None:
            for line in out.splitlines():
                cols = line.split()
                if len(cols) < 4:
                    continue
                local = cols[3]
                m = re.search(r":(\d+)$", local)
                if m:
                    try:
                        ports.add(int(m.group(1)))
                    except Exception:
                        pass
            return ports

    if shutil.which("netstat"):
        rc, out = _run(["netstat", "-ltn"], Path.cwd(), timeout=8)
        if rc is not None:
            for line in out.splitlines():
                m = re.search(r":(\d+)\s", line)
                if m:
                    try:
                        ports.add(int(m.group(1)))
                    except Exception:
                        pass
    return ports


def _http_probe(port: int, path: str = "/chat", timeout_sec: float = 2.5) -> tuple[bool, int | None, str]:
    url = f"http://127.0.0.1:{int(port)}{path}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            code = int(getattr(resp, "status", 0) or 0)
            return 200 <= code < 400, code, url
    except urllib.error.HTTPError as e:
        code = int(getattr(e, "code", 0) or 0)
        # Redirect responses can appear as HTTPError when redirects are disabled upstream.
        return 200 <= code < 400, code, url
    except Exception:
        return False, None, url


def _pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _check_python() -> list[CheckResult]:
    out: list[CheckResult] = []
    exe = sys.executable or ""
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    out.append(CheckResult("PASS", "python.runtime", f"python={exe} version={version}"))
    if sys.version_info < (3, 9):
        out.append(CheckResult("FAIL", "python.version", "Python version must be >= 3.9"))
    return out


def _check_required_files(repo_root: Path) -> list[CheckResult]:
    out: list[CheckResult] = []
    required = [
        "run_web.py",
        "start.sh",
        "stop.sh",
        "status.sh",
        "start.bat",
        "stop.bat",
        "status.bat",
        "status.ps1",
        "TindaAgent/Web/server.py",
        "TindaAgent/docs/CHANGELOG.md",
    ]
    for rel in required:
        p = repo_root / rel
        if p.exists():
            out.append(CheckResult("PASS", "file.exists", rel))
        else:
            out.append(CheckResult("FAIL", "file.missing", rel))
    return out


def _check_python_compile(repo_root: Path) -> list[CheckResult]:
    out: list[CheckResult] = []
    targets = [
        "run_web.py",
        "doctor.py",
        "TindaAgent/Web/server.py",
        "TindaAgent/Web/tool_runtime.py",
        "TindaAgent/Process/AI/client.py",
    ]
    for rel in targets:
        rc, text = _run([sys.executable, "-m", "py_compile", str(repo_root / rel)], repo_root, timeout=25)
        if rc == 0:
            out.append(CheckResult("PASS", "python.compile", rel))
        else:
            msg = (text or "").strip().splitlines()
            head = msg[-1] if msg else "compile failed"
            out.append(CheckResult("FAIL", "python.compile", f"{rel}: {head}"))
    return out


def _check_shell_scripts(repo_root: Path) -> list[CheckResult]:
    out: list[CheckResult] = []
    if os.name == "nt":
        return out
    for rel in ["start.sh", "stop.sh", "status.sh", "doctor.sh"]:
        p = repo_root / rel
        if not p.exists():
            out.append(CheckResult("FAIL", "shell.missing", rel))
            continue
        rc, text = _run(["bash", "-n", str(p)], repo_root, timeout=10)
        if rc == 0:
            out.append(CheckResult("PASS", "shell.syntax", rel))
        else:
            out.append(CheckResult("FAIL", "shell.syntax", f"{rel}: {(text or '').strip()}"))
        if os.access(str(p), os.X_OK):
            out.append(CheckResult("PASS", "shell.exec", rel))
        else:
            out.append(CheckResult("WARN", "shell.exec", f"{rel} is not executable", "Run: chmod +x *.sh"))
    return out


def _check_windows_scripts(repo_root: Path) -> list[CheckResult]:
    out: list[CheckResult] = []
    if os.name != "nt" and not _is_wsl():
        return out

    cmd_exe = shutil.which("cmd.exe")
    if not cmd_exe:
        out.append(CheckResult("WARN", "windows.bridge", "cmd.exe not found from current shell"))
        return out

    # start.bat has no --help mode and will really boot the server, so only smoke-check file existence.
    start_path = repo_root / "start.bat"
    if start_path.exists():
        out.append(CheckResult("PASS", "bat.exists", "start.bat"))
    else:
        out.append(CheckResult("FAIL", "bat.missing", "start.bat"))

    repo_win = _to_windows_path(repo_root)
    for rel, args in [
        ("stop.bat", "--help"),
        ("status.bat", "--help"),
        ("doctor.bat", "--help"),
    ]:
        p = repo_root / rel
        if not p.exists():
            out.append(CheckResult("FAIL", "bat.missing", rel))
            continue
        command = f"cd /d {repo_win} && {rel} {args}"
        rc, text = _run([cmd_exe, "/c", command], repo_root, timeout=20)
        if rc == 0:
            out.append(CheckResult("PASS", "bat.exec", f"{rel} {args}"))
        else:
            tail = (text or "").strip().splitlines()
            info = tail[-1] if tail else "failed"
            out.append(CheckResult("FAIL", "bat.exec", f"{rel} {args}: {info}"))
    return out


def _check_ports_and_http(repo_root: Path) -> list[CheckResult]:
    out: list[CheckResult] = []
    tracked = _read_tracked_ports(repo_root)
    listen = _find_listening_ports()

    if tracked:
        out.append(CheckResult("PASS", "ports.tracked", "tracked=" + " ".join(str(x) for x in tracked)))
    else:
        out.append(CheckResult("WARN", "ports.tracked", "no tracked ports in .tinda_ports.list"))

    if listen:
        sample = sorted(listen)[:15]
        out.append(CheckResult("PASS", "ports.listening", "listening_count=%d sample=%s" % (len(listen), " ".join(str(x) for x in sample))))
    else:
        out.append(CheckResult("WARN", "ports.listening", "no listening TCP ports detected"))

    if tracked:
        stale = [p for p in tracked if p not in listen]
        if stale:
            out.append(
                CheckResult(
                    "WARN",
                    "ports.stale",
                    "tracked but not listening=" + " ".join(str(x) for x in stale),
                    "Run: ./stop.sh --all && clear .tinda_ports.list if stale remains",
                )
            )

    candidate = []
    for p in tracked:
        if p in listen:
            candidate.append(p)
    if not candidate:
        for p in (8000, 8001, 8002):
            if p in listen:
                candidate.append(p)

    if not candidate:
        out.append(CheckResult("WARN", "http.probe", "no candidate listening tinda ports for HTTP probe"))
        return out

    for p in candidate[:3]:
        ok = False
        seen = []
        for path in ("/app", "/chat", "/"):
            passed, status, url = _http_probe(p, path=path)
            seen.append(f"{path}:{status if status is not None else 'ERR'}")
            if passed:
                ok = True
                out.append(CheckResult("PASS", "http.probe", f"{url} status={status}"))
                break
        if not ok:
            out.append(
                CheckResult(
                    "FAIL",
                    "http.probe",
                    f"port {p} HTTP probe failed ({', '.join(seen)})",
                    "If server claims running but probe fails, check firewall/proxy and host binding.",
                )
            )
    return out


def _check_startup_probe(repo_root: Path, timeout_sec: float = 30.0) -> list[CheckResult]:
    out: list[CheckResult] = []
    port = _pick_free_port()
    cmd = [sys.executable, "run_web.py", "--port", str(port), "--port-retries", "0", "--host", "0.0.0.0"]
    proc: subprocess.Popen[str] | None = None
    boot_ok = False
    startup_tail = ""
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        deadline = time.time() + float(timeout_sec)
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if _is_port_open("127.0.0.1", port, timeout_sec=0.25):
                boot_ok = True
                break
            time.sleep(0.25)

        if not boot_ok:
            # Avoid blocking read() on a still-running process: terminate first, then collect bounded output.
            rc = proc.poll()
            if rc is None:
                out.append(
                    CheckResult(
                        "WARN",
                        "startup.slow",
                        f"run_web.py still not listening on {port} after {timeout_sec:.0f}s",
                        "Possible WSL/Windows cross-env probe delay; retry doctor or run ./status.sh --show.",
                    )
                )
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                return out

            try:
                _, out_text = proc.communicate(timeout=3)
                startup_tail = (out_text or "")[-300:]
            except Exception:
                startup_tail = ""
                try:
                    proc.kill()
                    _, out_text = proc.communicate(timeout=2)
                    startup_tail = (out_text or "")[-300:]
                except Exception:
                    startup_tail = ""
            rc = proc.poll()
            out.append(
                CheckResult(
                    "FAIL",
                    "startup.boot",
                    f"run_web.py failed to listen on {port} (rc={rc})",
                    "Check DEEPSEEK_API_KEY and startup traceback. tail=" + startup_tail.replace("\n", " | "),
                )
            )
            return out

        out.append(CheckResult("PASS", "startup.boot", f"run_web.py listened on {port}"))
        passed, status, url = _http_probe(port, path="/chat", timeout_sec=3.0)
        if passed:
            out.append(CheckResult("PASS", "startup.http", f"{url} status={status}"))
        else:
            out.append(
                CheckResult(
                    "FAIL",
                    "startup.http",
                    f"startup probe failed for {url} status={status}",
                    "If this fails on Linux but works on Windows, verify WSL localhost forwarding and firewall.",
                )
            )
    except Exception as exc:
        out.append(CheckResult("FAIL", "startup.exception", f"{type(exc).__name__}: {exc}"))
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass
    return out


def _check_wsl_bridge(repo_root: Path) -> list[CheckResult]:
    out: list[CheckResult] = []
    if not _is_wsl():
        return out

    out.append(CheckResult("PASS", "wsl.detect", "WSL detected"))
    if shutil.which("cmd.exe"):
        out.append(CheckResult("PASS", "wsl.bridge", "cmd.exe available"))
    else:
        out.append(CheckResult("WARN", "wsl.bridge", "cmd.exe unavailable from WSL"))

    if shutil.which("powershell.exe") or shutil.which("pwsh.exe"):
        out.append(CheckResult("PASS", "wsl.bridge", "powershell bridge available"))
    else:
        out.append(CheckResult("WARN", "wsl.bridge", "powershell bridge unavailable from WSL"))

    tracked = _read_tracked_ports(repo_root)
    if tracked:
        p = tracked[0]
        out.append(
            CheckResult(
                "PASS",
                "wsl.hint",
                f"Open from Windows browser: http://127.0.0.1:{p}/chat",
                "If unreachable, run doctor and check FAIL/WARN startup.http + firewall rules.",
            )
        )
    return out


def _run_tests(repo_root: Path, pattern: str = "test_*.py") -> list[CheckResult]:
    out: list[CheckResult] = []
    cmd = [sys.executable, "-m", "unittest", "discover", "-s", "TindaAgent/Master", "-p", pattern, "-v"]
    rc, text = _run(cmd, repo_root, timeout=900)
    if rc == 0:
        out.append(CheckResult("PASS", "tests.unit", "unittest discover passed"))
    else:
        tail = " | ".join((text or "").splitlines()[-8:])
        out.append(CheckResult("FAIL", "tests.unit", f"unittest discover failed rc={rc}", hint=tail))
    return out


def _print_report(results: list[CheckResult], as_json: bool = False) -> int:
    fail = 0
    warn = 0
    if as_json:
        payload = {
            "summary": {
                "pass": sum(1 for r in results if r.level == "PASS"),
                "warn": sum(1 for r in results if r.level == "WARN"),
                "fail": sum(1 for r in results if r.level == "FAIL"),
            },
            "results": [asdict(r) for r in results],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1 if payload["summary"]["fail"] > 0 else 0

    for r in results:
        if r.level == "FAIL":
            fail += 1
        elif r.level == "WARN":
            warn += 1
        line = f"[{r.level}] {r.code}: {r.message}"
        print(line)
        if r.hint:
            print(f"  hint: {r.hint}")

    pcount = sum(1 for r in results if r.level == "PASS")
    print("")
    print(f"[SUMMARY] PASS={pcount} WARN={warn} FAIL={fail}")
    return 1 if fail > 0 else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="TindaAgent doctor")
    parser.add_argument("--skip-tests", action="store_true", help="skip unittest discover")
    parser.add_argument("--tests-pattern", default="test_*.py", help="unittest discover pattern")
    parser.add_argument("--no-startup-probe", action="store_true", help="skip temporary run_web startup probe")
    parser.add_argument("--json", action="store_true", help="output JSON report")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent

    results: list[CheckResult] = []
    results.extend(_check_python())
    results.extend(_check_required_files(repo_root))
    results.extend(_check_python_compile(repo_root))
    results.extend(_check_shell_scripts(repo_root))
    results.extend(_check_windows_scripts(repo_root))
    results.extend(_check_ports_and_http(repo_root))
    results.extend(_check_wsl_bridge(repo_root))

    if not args.no_startup_probe:
        results.extend(_check_startup_probe(repo_root))

    if not args.skip_tests:
        results.extend(_run_tests(repo_root, pattern=str(args.tests_pattern or "test_*.py")))

    return _print_report(results, as_json=bool(args.json))


if __name__ == "__main__":
    raise SystemExit(main())
