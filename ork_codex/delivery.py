"""Prepared evidence is not emitted evidence. Receipts finalize at the sink."""
import sqlite3


class DeliveryInfo(dict):
    def __init__(self, values, store):
        super().__init__(values)
        self.store = store
        self.finalized = False

    def confirm(self, byte_count):
        if self.finalized:
            return
        self.finalized = True
        try:
            self.store.finish_delivery(self["intervention"], "emitted", byte_count)
        except (OSError, sqlite3.Error):
            self.store.mark_accounting_gap()

    def abandon(self, reason="abandoned"):
        if self.finalized:
            return
        self.finalized = True
        if reason in {"write_failed", "flush_failed"}:
            self.store.mark_accounting_gap()
        try:
            self.store.finish_delivery(self["intervention"], reason)
        except (OSError, sqlite3.Error):
            self.store.mark_accounting_gap()


class RecoveryReceipt:
    def __init__(self, store, handle, context):
        self.store, self.handle, self.context = store, handle, context
        self.finalized = False
        self.recorded = None

    def confirm(self, byte_count):
        if self.finalized:
            return
        self.finalized = True
        self.recorded = self.store.charge_recovery(self.handle, byte_count, self.context)
        return self.recorded

    def abandon(self, reason="abandoned"):
        self.finalized = True
        if reason in {"write_failed", "flush_failed"}:
            self.store.mark_accounting_gap()


class ToolResult(dict):
    def __init__(self, values, receipt=None):
        super().__init__(values)
        self.receipt = receipt


class OutputDeliveryError(RuntimeError):
    def __init__(self, exit_code, artifact=None):
        self.exit_code = exit_code
        self.artifact = artifact
        super().__init__("command ran, but its output could not be delivered")
