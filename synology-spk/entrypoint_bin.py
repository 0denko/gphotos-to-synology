"""PyInstaller standalone binary entrypoint for native Synology DSM 7 SPK packages."""

from gphotos_synology.main import cli

if __name__ == "__main__":
    cli()
