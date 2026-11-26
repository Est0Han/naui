"""Project metadata shared by the CLI, About dialog and packaging."""
import os
from urllib.parse import urlparse

from . import __version__

NAME = "Naui"
DESCRIPTION = "Terminal, SSH, file editor and media previews"
LICENSE = "MIT"


def project_url():
    # No fabricated GitHub account. Distributors can supply their real URL.
    value = os.environ.get("NAUI_PROJECT_URL", "").strip()
    try:
        parsed = urlparse(value)
        valid = (parsed.scheme in {"https", "http"} and parsed.netloc
                 and not parsed.username and not parsed.password
                 and not any(character.isspace() for character in value))
    except ValueError:
        return ""
    return value if valid else ""


def version_string():
    return f"{NAME} {__version__}"
