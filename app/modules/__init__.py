"""Feature modules. Each exposes `router` from its routes.py."""
import importlib

MODULES = ["leads", "rank", "crm", "audit", "aivis", "clients", "gbp", "citations", "reports", "ask", "journal", "ai_platforms"]


def routers():
    return [importlib.import_module(f"app.modules.{m}.routes").router for m in MODULES]
