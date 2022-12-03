$(function () {
    function BgsTerminalViewModel(viewModels) {
      var self = this;

      self.terminal = viewModels[0];
      self.settings = viewModels[1];

      self.onBeforeBinding = function() {
        self.terminal.activeFilters(self.settings.settings.plugins.bettergrblsupport.activeFilters());

        self.terminal.activeFilters.subscribe(function (newValue) {
          self.settings.settings.plugins.bettergrblsupport.activeFilters(newValue);
          self.settings.saveData();
        });
      }
    };

    OCTOPRINT_VIEWMODELS.push([
        BgsTerminalViewModel,
        ["terminalViewModel","settingsViewModel"],
        []
    ]);
});
