"""Run the storage contract in a new localhost-only PostgreSQL cluster, then stop it."""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from tempfile import mkdtemp

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-bin", type=Path, help="Directory containing PostgreSQL tools")
    args = parser.parse_args()
    suffix = ".exe" if os.name == "nt" else ""
    commands = {}
    for name in ("initdb", "pg_ctl", "createdb", "postgres"):
        executable = (
            args.postgres_bin / f"{name}{suffix}" if args.postgres_bin else shutil.which(name)
        )
        if executable is None or not Path(executable).is_file():
            parser.error(f"{name} not found; provide --postgres-bin")
        commands[name] = str(executable)
    cache = ROOT / ".cache" / "postgres-runtime"
    cache.mkdir(parents=True, exist_ok=True)
    run = Path(mkdtemp(prefix="run-", dir=cache)).resolve()
    cluster = run / "cluster"
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = dict(os.environ)
    environment.pop("VERDICT_TEST_REDIS_URL", None)
    database_url = f"postgresql+psycopg://verdict_runtime@127.0.0.1:{port}/verdict_runtime"
    environment["VERDICT_TEST_DATABASE_URL"] = database_url
    environment["VERDICT_DATABASE_URL"] = database_url
    # Keep subprocess temporary files under this owned run as well.
    temporary = run / "temp"
    temporary.mkdir()
    environment.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary))

    def execute(command, *, check=True):
        return subprocess.run(command, cwd=ROOT, env=environment, check=check)

    version = subprocess.check_output([commands["postgres"], "--version"], text=True).strip()
    print(json.dumps({"postgres": version, "run_directory": str(run), "port": port}), flush=True)
    execute(
        [
            commands["initdb"],
            "-D",
            str(cluster),
            "-U",
            "verdict_runtime",
            "--auth-local=trust",
            "--auth-host=trust",
            "--encoding=UTF8",
            "--locale=C",
        ]
    )
    # This unique cluster is owned by this invocation. Never attach to or stop an
    # existing cluster, and never recursively remove database directories.
    started = False
    try:
        execute(
            [
                commands["pg_ctl"],
                "-D",
                str(cluster),
                "-l",
                str(run / "postgres.log"),
                "-o",
                f"-h 127.0.0.1 -p {port}",
                "-w",
                "-t",
                "30",
                "start",
            ]
        )
        started = True
        execute(
            [
                commands["createdb"],
                "-h",
                "127.0.0.1",
                "-p",
                str(port),
                "-U",
                "verdict_runtime",
                "verdict_runtime",
            ]
        )
        result = execute(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_postgres.py",
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(run / "pytest"),
            ],
            check=False,
        )
        return result.returncode
    finally:
        # Also stop after a startup command reports failure with a server already
        # running (for example, a timeout immediately after startup).
        if started or (cluster / "postmaster.pid").exists():
            stopped = execute(
                [commands["pg_ctl"], "-D", str(cluster), "-m", "fast", "-w", "-t", "30", "stop"],
                check=False,
            )
            if stopped.returncode:
                raise RuntimeError(f"temporary PostgreSQL failed to stop; inspect {run}")
        print(json.dumps({"stopped": True, "retained_logs": str(run)}), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
