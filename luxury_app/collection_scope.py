"""Choose the inventory collection from the authenticated role."""

from flask import has_request_context, session

from .auth import ROLE_STAFF


class RoleScopedItems:
    """Proxy Admin to items and Staff to items_view without duplicating routes."""

    def __init__(self, admin_items, staff_items):
        self.admin_items = admin_items
        self.staff_items = staff_items

    def _selected(self):
        if has_request_context() and session.get("role") == ROLE_STAFF:
            return self.staff_items
        return self.admin_items

    @property
    def database(self):
        return self._selected().database

    @property
    def name(self):
        return self._selected().name

    def __getattr__(self, name):
        return getattr(self._selected(), name)
