"""Ducky-Craft Discord-bot."""
import os

# Wordt bij het bouwen van de Docker-image gezet (release-tag of commit).
__version__ = os.environ.get("APP_VERSION", "dev")
