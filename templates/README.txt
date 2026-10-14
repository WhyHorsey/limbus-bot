This folder documents the template names. The real templates are stored in your user config folder, shown in the app and by `python micro_manager.py --doctor`:

  Windows: %APPDATA%\limbus-micro-manager\templates
  Linux:   ~/.config/limbus-micro-manager/templates

State templates (configured in config.json):
  state_battle.png
  state_map.png
  state_gift_select.png
  state_shop.png
  state_event.png
  state_result.png
  state_run_complete.png
  state_lux_stage_select.png
  state_mode_select.png
  state_gift_upgrade.png

Common action templates:
  button_continue.png
  button_start_mirror.png
  button_start_lux_exp.png
  button_start_lux_thread.png
  button_retry_lux.png
  button_enter_mirror_again.png
  button_leave_shop.png
  button_gift_fallback.png
  button_event_fallback.png
  button_confirm_upgrade.png

Mirror Dungeon reachable-node templates:
  node_event_available.png
  node_shop_available.png
  node_rest_available.png
  node_focused_available.png
  node_normal_available.png
  node_risky_available.png

Gift/upgrade/event templates may use any filename. The calibration wizard can name and automatically add them to the relevant priority list.

Calibration guidelines:
- Capture from the same game language, UI scale and display arrangement you'll use later.
- For state templates, crop a distinctive and stable heading/panel; for battle, prefer a visible skill-selection/Win Rate indicator over persistent battle art.
- For action templates, crop the specific button rather than a large surrounding panel.
- For node templates, crop the available/selectable state, not an icon shown on every node.
- For gift/upgrade/event templates, crop a distinctive card detail that stays stable.
- Analyze the current screen and keep input disabled until detections have been verified.
- The calibration wizard uses an in-app crop window, so it does not depend on OpenCV's separate `selectROI` GUI.
