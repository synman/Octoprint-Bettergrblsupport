

class ZProbe:
    _plugin = None
    _hook = None

    def __init__(self, _plugin, _hook):
        self._plugin = _plugin
        self._hook = _hook
        _plugin._logger.info("ZProbe initialized")

    def notify(self, notifications):
        for notification in notifications:
            if "Check Mode" in notification:
                self._plugin._logger.info("notified: " + notification)
                self._hook(self._plugin, notification)

    def teardown(self):
        _hook = None
        _plugin = None
