Put your own cropped UI templates in this directory.

State templates (configured in config.json):
  state_battle.png
  state_map.png
  state_gift_select.png
  state_shop.png
  state_event.png
  state_result.png
  state_run_complete.png
  state_lux_stage_select.png

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

Mirror Dungeon reachable-node templates:
  node_event_available.png
  node_shop_available.png
  node_rest_available.png
  node_focused_available.png
  node_normal_available.png
  node_risky_available.png

Gift/upgrade/event templates can use any filename, as long as the exact filename
is entered in the GUI priorities. Example: gift_priority_1.png, upgrade_priority_1.png,
event_best.png.

Template guidelines:
- Use screenshots from the same game language and UI scale you intend to run.
- Crop a small, distinctive, stable area. Avoid animated effects and changing numbers.
- For state templates, crop a heading or stable panel that identifies the current screen.
- For node templates, crop the *available/selectable* variant, not the same icon shown on every node.
- Test with clicks disabled first. A wrong or overly generic template can misidentify a screen.

The GUI also has a “Capture template…” button. It minimizes the app, captures
the screen after a short delay, and opens a crop selector. Bring the game to the
foreground as soon as the app minimizes. Save the crop using a filename from
the lists above; the file is stored in the user config's templates folder.
