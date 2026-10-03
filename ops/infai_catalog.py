"""Compatibility entry point; exporter is packaged in the runtime image."""

from app.providers.infai_export import main
from app.providers.infai_export import write_report as write_report

if __name__ == "__main__":
    main()
