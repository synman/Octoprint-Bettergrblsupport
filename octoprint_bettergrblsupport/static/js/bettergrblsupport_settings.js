/*
 * View model for OctoPrint-Bettergrblsupport
 *
 * Author: Shell M. Shrader
 * License: Apache 2.0
 */
$(function() {
    function BettergrblsupportSettingsViewModel(parameters) {
      var self = this;
      // var $body = $('body');

      // assign the injected parameters, e.g.:
      self.settingsViewModel = parameters[0];
      self.loginState = parameters[1];

      self.settings = undefined;

      self.is_printing = ko.observable(false);
      self.is_operational = ko.observable(false);

      self.probeEnabled = ko.observable(false);
      self.isMultiPoint = ko.observable(false);

      self.fluidSettings = ko.observableArray([]);
      self.updateFluid = function(key, value, oldvalue) {
        if (value != oldvalue) {
          self.settings.plugins.bettergrblsupport.fluidSettings[key](value);
        }
      };

      self.grblSettings = ko.observableArray([]);

      // the message of an error answer ({"error": ...}), else its raw text
      self.errorText = function(xhr) {
        try {
          var error = JSON.parse(xhr.responseText).error;
          if (error) return error;
        } catch (e) {}
        return xhr.responseText;
      };

      self.findGrblSetting = function(id) {
        var items = self.grblSettings();
        for (var i in items) {
          if (items[i].id == id) return items[i];
        }
        return undefined;
      };

      // value is the row's observable; the table keeps the last value Grbl confirmed in oldvalue
      self.updateSetting = function(id, value, oldvalue) {
        var item = self.findGrblSetting(id);
        if (item == undefined) return;
        var newvalue = ko.unwrap(value);

        if (self.is_printing()) {
          item.value(oldvalue);
          return;
        }

        if (newvalue != oldvalue) {
          $.ajax({
            url: API_BASEURL + "plugin/bettergrblsupport",
            type: "POST",
            dataType: "json",
            data: JSON.stringify({
              command: "updateGrblSetting",
              id: id,
              value: newvalue
            }),
            contentType: "application/json; charset=UTF-8",
            success: function(data) {
              // Grbl acknowledged it
              item.oldvalue = newvalue;

              new PNotify({
                title: "Grbl Settings Update",
                text: "$" + id + " has been set to " + newvalue,
                text_escape: true,
                hide: true,
                animation: "fade",
                animateSpeed: "slow",
                mouseReset: true,
                delay: 10000,
                buttons: {
                  sticker: true,
                  closer: true
                },
                type: "success"
              });
            },
            error: function (data, status) {
              // Grbl refused it (or never answered): the table goes back to the last confirmed value
              item.value(oldvalue);

              new PNotify({
                title: "Grbl setting update failed!",
                text: "$" + id + ": " + self.errorText(data),
                text_escape: true,
                hide: true,
                buttons: {
                  sticker: false,
                  closer: true
                },
                type: "error"
              });
            }
          });
        }
      };

      self.backupSettings = function() {
        $.ajax({
          url: API_BASEURL + "plugin/bettergrblsupport",
          type: "POST",
          dataType: "json",
          data: JSON.stringify({
            command: "backupGrblSettings"
          }),
          contentType: "application/json; charset=UTF-8",
          success: function(data) {
            // the new backup enables Restore without waiting for the settings to be re-read
            self.settings.plugins.bettergrblsupport.grblSettingsBackup(data["res"]);

            new PNotify({
              title: "Grbl Settings Backup",
              text: "The backup has been completed successfully.",
              hide: false,
              buttons: {
                sticker: true,
                closer: true
              },
              type: "success"
            });
          },
          error: function (data, status) {
            new PNotify({
              title: "Grbl Settings Backup",
              text: data.responseText,
              text_escape: true,
              hide: true,
              buttons: {
                sticker: false,
                closer: true
              },
              type: "error"
            });
          }
        });
      };

      self.restoreSettings = function() {
        $.ajax({
          url: API_BASEURL + "plugin/bettergrblsupport",
          type: "POST",
          dataType: "json",
          data: JSON.stringify({
            command: "restoreGrblSettings"
          }),
          contentType: "application/json; charset=UTF-8",
          success: function(data) {
            new PNotify({
              title: "Grbl Settings Restore",
              text: "The restore has been completed successfully.",
              hide: true,
              buttons: {
                sticker: true,
                closer: true
              },
              type: "success"
            });
            self.pushGrblSettings(data["res"]);
          },
          error: function (data, status) {
            new PNotify({
              title: "Grbl Settings Restore",
              text: data.responseText,
              text_escape: true,
              hide: true,
              buttons: {
                sticker: false,
                closer: true
              },
              type: "error"
            });
          }
        });
      };

      self.toggleAdvanced = function() {
        var advanced = document.getElementById("settings_grbl_options")
        advanced.style.display = advanced.style.display == "none" ? "" : "none";
      };

      self.onBeforeBinding = function() {
        // initialize stuff here
        self.settings = self.settingsViewModel.settings;

        var settingsText = self.settings.plugins.bettergrblsupport.grblSettingsText();
        if (settingsText != null) {
          self.pushGrblSettings(settingsText);
        }

        // test the new value: a page opened before Grbl ever reported its settings starts with null
        self.settings.plugins.bettergrblsupport.grblSettingsText.subscribe(function(newValue) {
          if (newValue != null) {
            self.pushGrblSettings(newValue);
          }
        });

        self.probeEnabled(self.settings.plugins.bettergrblsupport.zprobeMethod() != "NONE");
        self.isMultiPoint(self.settings.plugins.bettergrblsupport.zprobeMethod() == "MULTI");

        self.settings.plugins.bettergrblsupport.zprobeMethod.subscribe(function(newValue) {
          if (newValue == "MULTI") {
            self.isMultiPoint(true);
            self.probeEnabled(true);
          } else {
            if (newValue == "SIMPLE") {
              self.isMultiPoint(false);
              self.probeEnabled(true);
            } else {
              self.isMultiPoint(false);
              self.probeEnabled(false);
            }
          }
        });
      };

      self.pushGrblSettings = function(grblSettingsText) {
        self.grblSettings.removeAll();

        var grblSettings = grblSettingsText.split("||");
        var settingsSize = grblSettings.length - 1;

        for (var i = 0; i < settingsSize; i++) {
          var setting = grblSettings[i].split("|");

          self.grblSettings.push({
              id: setting[0],
              value: ko.observable(setting[1]),
              oldvalue: setting[1],
              description: setting[2]
          });
        }
      };

    self.mapFluidToArray = function(fluid) {
        var result = [];
        for (var key in fluid) {
          result.push({ key: key, value: fluid[key](), oldvalue: fluid[key]() }); 
        }
        
        return result;
    };

      // establish our fluid settings state
      self.onSettingsShown = function() {
        if (self.settings.plugins.bettergrblsupport.fluidYaml()) {
          self.fluidSettings(self.mapFluidToArray(self.settings.plugins.bettergrblsupport.fluidSettings));
        }  
      };

      // move our updated fluid settings to our plugin settings
      self.onSettingsBeforeSave = function () {
        if (self.settings.plugins.bettergrblsupport.fluidYaml()) {
          for (var i in self.fluidSettings()) {
            if (self.fluidSettings()[i].value != self.fluidSettings()[i].oldvalue) {
              console.log("updating key=" + self.fluidSettings()[i].key + " value=" + self.fluidSettings()[i].value + " oldvalue=" + self.fluidSettings()[i].oldvalue);
              self.settings.plugins.bettergrblsupport.fluidSettings[self.fluidSettings()[i].key](self.fluidSettings()[i].value);
            }
          }
        }
      };

      self.fromCurrentData = function (data) {
          self._processStateData(data.state);
      };

      self.fromHistoryData = function (data) {
          self._processStateData(data.state);
      };

      self._processStateData = function (data) {
          self.is_printing(data.flags.printing);
          self.is_operational(data.flags.operational);
      };

      self.handleFocus = function (event, type, item) {
        window.setTimeout(function () {
            event.target.select();
        }, 0);
      };

      ko.bindingHandlers.numeric = {
          init: function (element, valueAccessor) {
              $(element).on("keydown", function (event) {
                  // Allow: backspace, delete, tab, escape, and enter
                  if (event.keyCode == 46 || event.keyCode == 8 || event.keyCode == 9 || event.keyCode == 27 || event.keyCode == 13 ||
                      // Allow: Ctrl+A
                      (event.keyCode == 65 && event.ctrlKey === true) ||
                      // Allow: . ,
                      (event.keyCode == 188 || event.keyCode == 190 || event.keyCode == 110) ||
                      // Allow: home, end, left, right
                      (event.keyCode >= 35 && event.keyCode <= 39)) {
                      // let it happen, don't do anything
                      return;
                  }
                  else {
                      // Ensure that it is a number and stop the keypress
                      if (event.shiftKey || (event.keyCode < 48 || event.keyCode > 57) && (event.keyCode < 96 || event.keyCode > 105)) {
                          event.preventDefault();
                      }
                  }
              });
          }
      };
    }

    /* view model class, parameters for constructor, container to bind to
     * Please see http://docs.octoprint.org/en/master/plugins/viewmodels.html#registering-custom-viewmodels for more details
     * and a full list of the available options.
     */
    // OCTOPRINT_VIEWMODELS.push({
    //     construct: BettergrblsupportViewModel,
    //     // ViewModels your plugin depends on, e.g. loginStateViewModel, settingsViewModel, ...
    //     dependencies: [ /* "loginStateViewModel", "settingsViewModel" */ ],
    //     // Elements to bind to, e.g. #settings_plugin_bettergrblsupport, #tab_plugin_bettergrblsupport, ...
    //     elements: [ /* ... */ ]
    // });

    OCTOPRINT_VIEWMODELS.push([
      BettergrblsupportSettingsViewModel,
      [ "settingsViewModel", "loginStateViewModel" ],
        "#settings_plugin_bettergrblsupport"
      ]);
});
