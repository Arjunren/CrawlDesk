"""Build and verify a portable Windows x64 release using PyInstaller."""
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop import APP_VERSION


def main():
    if sys.platform != "win32" or platform.machine().upper() not in {"AMD64", "X86_64"}:
        raise SystemExit("Build this Windows x64 release on Windows x64.")
    import PyInstaller
    output = ROOT / "dist"
    work = ROOT / "build"
    work.mkdir(exist_ok=True)
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
               "--windowed", "--name", "CrawlDesk", "--add-data", f"{ROOT / 'ui'};ui",
               "--icon", str(ROOT / "assets" / "crawldesk.ico"),
               "--exclude-module", "playwright", "--exclude-module", "pytest",
               "--distpath", str(output), "--workpath", str(work / "pyinstaller"),
               "--specpath", str(work), str(ROOT / "desktop.py")]
    subprocess.run(command, cwd=ROOT, check=True)
    executable = output / "CrawlDesk.exe"
    report = work / "frozen-smoke.json"
    if report.exists():
        report.unlink()
    subprocess.run([str(executable), "--smoke-test", str(report)], cwd=ROOT, check=True,
                   timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
    verification = json.loads(report.read_text(encoding="utf-8"))
    if not verification.get("ok") or not verification.get("frozen"):
        raise SystemExit("Frozen app verification failed")
    archive = output / f"CrawlDesk-{APP_VERSION}-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as package:
        package.write(executable, "CrawlDesk.exe")
        package.write(ROOT / "docs" / "WINDOWS-RELEASE.txt", "README.txt")
        package.write(ROOT / "LICENSE", "LICENSE")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    manifest = {"version": APP_VERSION, "platform": "windows-x64", "source_commit": commit,
                "python": platform.python_version(), "pyinstaller": PyInstaller.__version__,
                "verification": verification}
    manifest_path = output / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    assets = [executable, archive, manifest_path]
    checksums = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in assets)
    (output / "SHA256SUMS.txt").write_text(checksums, encoding="utf-8")
    print(json.dumps({"ok":True, "source_commit":commit, "assets":[path.name for path in assets],
                      "verification":verification}, indent=2))


if __name__ == "__main__":
    main()
